# -*- coding: utf-8 -*-

from AdpCommon  import *
from AdpConfig  import *
from AdpLogging import *

import MCS2
from DeviceMonitor import ROACH2Device
from ConsumerThread import ConsumerThread
from SequenceDict import SequenceDict
from ThreadPool import ThreadPool
from ThreadPool import ObjectPool
#from Cache      import threadsafe_lru_cache as lru_cache
from Cache      import lru_cache_method
from AdpRoach   import AdpRoach
from iptools    import *

import ISC

from Queue import Queue
import numpy as np
import time
import math
from collections import defaultdict
import logging
import struct
import subprocess
import datetime
import zmq
# TODO: This caused weird hangs when launching on two servers, so I
#         moved it into the local function as a WAR.
## Note: paramiko must be pip installed (it's also included with fabric)
#import paramiko # For ssh'ing into roaches to call reboot
import threading
import socket # For socket.error

#from paramiko import py3compat
## dirty hack to fix threading import lock (issue 104) by preloading module
#py3compat.u("dirty hack")
#import paramiko
##''.decode('utf-8')  # dirty fix

#_hack_ssh = paramiko.SSHClient()
#_hack_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#try:
#	_hack_ssh.connect("127.0.0.1", timeout=0.001)
#except:
#	pass

__version__    = "0.2"
__author__     = "Ben Barsdell, Daniel Price, Jayce Dowell"
__copyright__  = "Copyright 2015, The LWA-SV Project"
__credits__    = ["Ben Barsdell", "Daniel Price", "Jayce Dowell"]
__license__    = "Apache v2"
__maintainer__ = "Jayce Dowell"
__email__      = "jdowell at unm"
__status__     = "Development"

# Global shared resources
#_g_thread_pool = ThreadPool()
_g_zmqctx      = zmq.Context()

def wait_until_utc_sec(utcstr):
	cur_time = datetime.datetime.utcnow().strftime(DATE_FORMAT)
	while cur_time != utcstr:
		time.sleep(0.01)
		cur_time = datetime.datetime.utcnow().strftime(DATE_FORMAT)

class SlotCommandProcessor(object):
	def __init__(self, cmd_code, cmd_parser, exec_delay=2):
		self.cmd_sequence = defaultdict(list)
		self.exec_delay   = exec_delay
		self.cmd_code     = cmd_code
		self.cmd_parser   = cmd_parser
		
	@ISC.logException
	def process_command(self, msg):
		assert( msg.cmd == self.cmd_code )
		exec_slot = msg.slot + self.exec_delay
		self.cmd_sequence[exec_slot].append(self.cmd_parser(msg))
		return 0
		
	@ISC.logException
	def execute_commands(self, slot):
		try:
			cmds = self.cmd_sequence.pop(slot)
		except KeyError:
			return
		return self.execute(cmds)


class TbnCommand(object):
	def __init__(self, msg):
		self.freq, self.filt, self.gain \
		    = struct.unpack('>fhh', msg.data)
		# TODO: Check allowed range of freq
		assert( 1 <= self.filt <= 11 )
		assert( 0 <= self.gain <= 30 )


class Tbn(SlotCommandProcessor):
	def __init__(self, config, log, messenger, servers, roaches):
		SlotCommandProcessor.__init__(self, 'TBN', TbnCommand)
		self.config  = config
		self.log     = log
		self.messenger = messenger
		self.servers = servers
		self.roaches = roaches
		self.cur_freq = self.cur_filt = self.cur_gain = 0
		
	def tune(self, freq=38.00e6, filt=1, gain=1):
		## Convert to the DP frequency scale
		freq = FS * int(freq / FS * 2**32) / 2**32
		
		self.log.info("Tuning TBN:   freq=%f,filt=%i,gain=%i" % (freq,filt,gain))
		rets = self.roaches.tune_tbn(freq)
		self.cur_freq = freq
		self.cur_filt = filt
		self.cur_gain = gain
		return rets
		
	def start(self, freq=59.98e6, filt=1, gain=1):
		## Convert to the DP frequency scale
		freq = FS * int(freq / FS * 2**32) / 2**32
		
		self.log.info("Starting TBN: freq=%f,filt=%i,gain=%i" % (freq,filt,gain))
		## TODO: Check whether pausing the data flow is necessary
		#self.roaches.disable_tbn_data()
		rets = self.tune(freq, filt, gain)
		#time.sleep(1.1)
		#rets = self.roaches.enable_tbn_data()
		
		self.messenger.tbnConfig(freq, filt, gain)
		
		return rets
		
	def execute(self, cmds):
		for cmd in cmds:
			self.start(cmd.freq, cmd.filt, cmd.gain)
			
	def stop(self):
		self.log.info("Stopping TBN")
		self.roaches.disable_tbn_data()
		self.log.info("TBN stopped")
		return 0


class DrxCommand(object):
	def __init__(self, msg):
		self.tuning, self.freq, self.filt, self.gain \
		    = struct.unpack('>BfBh', msg.data)
		assert( 1 <= self.tuning <= 2 )
		# TODO: Check allowed range of freq
		assert( 0 <= self.filt   <= 8 )
		assert( 0 <= self.gain   <= 15 )


class Drx(SlotCommandProcessor):
	def __init__(self, config, log, messenger, servers, roaches):
		SlotCommandProcessor.__init__(self, 'DRX', DrxCommand)
		self.config  = config
		self.log     = log
		self.messenger = messenger
		self.servers = servers
		self.roaches = roaches
		self.ntuning = 2
		self.cur_freq = [0]*self.ntuning
		self.cur_filt = [0]*self.ntuning
		self.cur_gain = [0]*self.ntuning
		
	def tune(self, tuning=0, freq=38.00e6, filt=1, gain=1):
		## Convert to the DP frequency scale
		freq = FS * int(freq / FS * 2**32) / 2**32
		
		self.log.info("Tuning DRX %i: freq=%f,filt=%i,gain=%i" % (tuning,freq,filt,gain))
		rets = self.roaches.tune_drx(tuning, freq)
		
		self.cur_freq[tuning] = freq
		self.cur_filt[tuning] = filt
		self.cur_gain[tuning] = gain
		
		return rets
		
	def start(self, tuning=0, freq=59.98e6, filt=1, gain=1):
		## Convert to the DP frequency scale
		freq = FS * int(freq / FS * 2**32) / 2**32
		
		self.log.info("Starting DRX %i data: freq=%f,filt=%i,gain=%i" % (tuning, freq,filt,gain))
		### TODO: Check whether pausing the data flow is necessary
		#self.roaches.disable_drx_data(tuning)
		rets = self.tune(tuning, freq, filt, gain)
		#time.sleep(1.1)
		#rets = self.roaches.enable_drx_data(tuning)
		
		self.messenger.drxConfig(tuning, freq, filt, gain)
		
		return rets
		
	def execute(self, cmds):
		for cmd in cmds:
			# Note: Converts from 1-based to 0-based tuning
			self.start(cmd.tuning-1, cmd.freq, cmd.filt, cmd.gain)
			
	def stop(self):
		self.log.info("Stopping DRX data")
		self.roaches.disable_drx_data(0)
		self.roaches.disable_drx_data(1)
		return 0


class TbfCommand(object):
	@ISC.logException
	def __init__(self, msg):
		self.bits, self.trigger, self.samples, self.mask \
		    = struct.unpack('>Biiq', msg.data)


class Tbf(SlotCommandProcessor):
	@ISC.logException
	def __init__(self, config, log, messenger, servers, roaches):
		SlotCommandProcessor.__init__(self, 'TBF', TbfCommand)
		self.config  = config
		self.log     = log
		self.messenger = messenger
		self.servers = servers
		self.roaches = roaches
		self.cur_bits = self.cur_trigger = self.cur_samples = self.cur_mask = 0
		
	@ISC.logException
	def start(self, bits, trigger, samples, mask):
		self.log.info('Starting TBF: bits=%i, trigger=%i, samples=%i, mask=%i' % (bits, trigger, samples, mask))
		
		self.messenger.trigger(trigger, samples, mask)
		
		return True
		
	@ISC.logException
	def execute(self, cmds):
		for cmd in cmds:
			self.start(cmd.bits, cmd.trigger, cmd.samples, cmd.mask)
			
	def stop(self):
		return False


class BamCommand(object):
	@ISC.logException
	def __init__(self, msg):
		self.beam = struct.unpack('>H', msg.data[0:2])[0]
		self.delays = np.ndarray((512,), dtype='>H', buffer=msg.data[2:1026])
		self.gains = np.ndarray((256,2,2), dtype='>H', buffer=msg.data[1026:3074])
		self.tuning, self.subslot = struct.unpack('>BB', msg.data[3074:3076])


class Bam(SlotCommandProcessor):
	@ISC.logException
	def __init__(self, config, log, messenger, servers, roaches):
		SlotCommandProcessor.__init__(self, 'BAM', BamCommand)
		self.config  = config
		self.log     = log
		self.messenger = messenger
		self.servers = servers
		self.roaches = roaches
		self.ntuning = 2
		self.cur_beam = [0]*self.ntuning
		self.cur_delays = [[0 for i in xrange(512)]]*self.ntuning
		self.cur_gains = [[0 for i in xrange(1024)]]*self.ntuning
		self.cur_tuning = [0]*self.ntuning
		
	@ISC.logException
	def start(self, beam, delays, gains, tuning, subslot):
		self.log.info("Setting BAM: beam=%i, tuning=%i, subslot=%i" % (beam, tuning, subslot))
		
		self.messenger.bamConfig(beam, delays, gains, tuning, subslot)
		
		return True
		
	@ISC.logException
	def execute(self, cmds):
		for cmd in cmds:
			# Note: Converts from 1-based to 0-based tuning
			self.start(cmd.beam, cmd.delays, cmd.gains, cmd.tuning-1, cmd.subslot)
			
	def stop(self):
		return False


class CorCommand(object):
	@ISC.logException
	def __init__(self, msg):
		self.navg, self.tuning, self.gain, self.subslot \
		    = struct.unpack('>iQhB', msg)


class Cor(SlotCommandProcessor):
	@ISC.logException
	def __init__(self, config, log, messenger, servers, roaches):
		SlotCommandProcessor.__init__(self, 'COR', CorCommand)
		self.config  = config
		self.log     = log
		self.messenger = messenger
		self.servers = servers
		self.roaches = roaches
		self.ntuning = 2
		self.cur_navg = [0]*self.ntuning
		self.cur_tuning = [0]*self.ntuning
		self.cur_gain = [0]*self.ntuning
		
	@ISC.logException
	def start(self, navg, tuning, gain, subslot):
		self.log.info("Setting COR: navg=%i, tuning=%i, gain=%i, subslot=%i" % (navg, tuning, gain, subslot))
		
		self.messenger.corConfig(navg, tuning, gain, subslot)
		
		return True
		
	@ISC.logException
	def execute(self, cmds):
		for cmd in cmds:
			# Note: Converts from 1-based to 0-based tuning
			self.start(cmd.navg, cmd.tuning-1, cmd.gain, cmd.subslot)
			
	def stop(self):
		return False


"""
class FstCommand(object):
	def __init__(self, msg):
		self.index = int(struct.unpack('>h', (msg.data[0:2]))[0])
		self.coefs = np.ndarray((16,32), dtype='>h', buffer=msg.data[2:])
class Fst(object):
	def __init__(self, config, log,
	             nupdate_save=5):
		self.config = config
		self.log    = log
		hosts = config['server_hosts']
		ports = config['fst']['control_ports']
		self.addrs = ['tcp://%s:%i'%(hosts[i/2],ports[i%2]) \
		              for i in xrange(len(hosts)*len(ports))]
		self.socks = ObjectPool([_g_zmqctx.socket(zmq.REQ) \
		                         for _ in self.addrs])
		for sock,addr in zip(self.socks,self.addrs):
			try: sock.connect(addr)
			except zmq.error.ZMQError:
				self.log.error("Invalid or non-existent address: %s" %
				                   addr)
				# TODO: How to bail out?
		self.exec_delay = 2
		self.cmd_sequence = defaultdict(list)
		self.fir_coefs = SequenceDict(lambda : np.ones((NSTAND,NPOL,
		                                                FIR_NFINE,FIR_NCOEF),
		                                               dtype=np.int16),
		                              maxlen=nupdate_save)
		self.fir_coefs[0][...] = self._load_default_fir_coefs()
		#self.future_pool = FuturePool(len(self.socks))
	def _load_default_fir_coefs(self):
		nfine = self.fir_coefs[-1].shape[-2]
		ncoef = self.fir_coefs[-1].shape[-1]
		fir_coefs = np.fromfile(self.config['fst']['default_coeffs'],
		                        dtype='>h').reshape(nfine,ncoef)
		return fir_coefs[None,None,:,:]
	def process_command(self, msg):
		assert( msg.cmd == 'FST' )
		exec_slot = msg.slot + self.exec_delay
		self.cmd_sequence[exec_slot].append(FstCommand(msg))
	def execute_commands(self, slot):
		try:
			cmds = self.cmd_sequence.pop(slot)
		except KeyError:
			return
		# Start with current coefs
		self.fir_coefs[slot][...] = self.fir_coefs.at(slot-1)
		# Merge updates into the set of coefficients
		for cmd in cmds:
			if cmd.index == -1:
				self.fir_coefs[slot][...] = self._load_default_fir_coefs()
			elif cmd.index == 0:
				# Apply same coefs to all inputs
				self.fir_coefs[slot][...] = cmd.coefs[None,None,:,:]
			else:
				stand = (cmd.index-1) / 2
				pol   = (cmd.index-1) % 2
				self.fir_coefs[slot][stand,pol] = cmd.coefs
		self._send_update(slot)
	def get_fir_coefs(self, slot):
		# Access history of updates
		return self.fir_coefs.at(slot)
	def _send_update(self, slot):
		weights = get_freq_domain_filter(self.fir_coefs[slot])
		# weights: [stand,pol,chan] complex64
		weights = weights.transpose(2,0,1)
		# weights: [chan,stand,pol] complex64
		weights /= weights.max() # Normalise to max DC gain of 1.0
		# Send update to pipelines
		# Note: We send all weights to all servers and let them extract
		#         the channels they need, rather than trying to keep
		#         track of which servers have which channels from here.
		# TODO: If msg traffic ever becomes a problem, could probably
		#         use fp16 instead of fp32 for these.
		#hdr  = struct.pack('@iihc', slot, NCHAN, NSTAND, NPOL)
		hdr = json.dumps({'slot':   slot,
		                  'nchan':  NCHAN,
		                  'nstand': NSTAND,
		                  'npol':   NPOL})
		data = weights.astype(np.complex64).tobytes()
		msg  = hdr+data

		self.socks.send_multipart([hdr, data])
		replies = self.socks.recv_json()
		#def send_msg(sock):
		#	sock.send_multipart([hdr, data])
		#	# TODO: Add receive timeout
		#	return sock.recv_json()
		#for sock in self.socks:
		#	self.future_pool.add_task(send_msg, sock)
		#replies = self.future_pool.wait()

		for reply,addr in zip(replies,self.addrs):
			if reply['status'] < 0:
				self.log.error("Gain update failed "
				               "for address %s: (%i) %s" %
				               addr, reply['status'], reply['info'])
"""


# Special response packing functions
def pack_reply_CMD_STAT(slot, cmds):
	ncmd_max = 606
	cmds = cmds[:ncmd_max]
	fmt = '>LH%dL%dB' % (len(cmds), len(cmds))
	responseParts = [slot, len(cmds)]
	responseParts.extend( [cmd[1] for cmd in cmds] )
	responseParts.extend( [cmd[2] for cmd in cmds] )
	return struct.pack(fmt, *responseParts)


def truncate_message(s, n):
		return s if len(s) <= n else s[:n-3] + '...'


def pretty_print_bytes(bytestring):
	return ' '.join(['%02x' % ord(i) for i in bytestring])


# HACK TESTING
#lock = threading.Lock()


class AdpServerMonitorClient(object):
	def __init__(self, config, log, host, timeout=0.1):
		self.config = config
		self.log  = log
		self.host = host
		self.host_ipmi = self.host + "-ipmi"
		self.port = config['mcs']['server']['local_port']
		self.sock = _g_zmqctx.socket(zmq.REQ)
		addr = 'tcp://%s:%i' % (self.host,self.port)
		try: self.sock.connect(addr)
		except zmq.error.ZMQError:
			self.log.error("Invalid or non-existent address: %s" % addr)
		self.sock.SNDTIMEO = int(timeout*1000)
		self.sock.RCVTIMEO = int(timeout*1000)
		
	def read_sensors(self):
		ret = self._ipmi_command('sdr')
		sensors = {}
		for line in ret.split('\n'):
			if '|' not in line:
				continue
			cols = [col.strip() for col in line.split('|')]
			key = cols[0]
			val = cols[1].split()[0]
			sensors[key] = val
		return sensors
		
	@lru_cache_method(maxsize=4)
	def get_temperatures(self, slot):
		try:
			sensors = self.read_sensors()
			return {key: float(sensors[key])
			        for key in self.config['server']['temperatures']
			        if  key in sensors}
		except:
			return {'error': float('nan')}
			
	@lru_cache_method(maxsize=4)
	def get_status(self, slot):
		return self._request('STAT')
		
	@lru_cache_method(maxsize=4)
	def get_info(self, slot):
		return self._request('INFO')
		
	@lru_cache_method(maxsize=4)
	def get_software(self, slot):
		return self._request('SOFTWARE')
		
	def _request(self, query):
		try:
			self.sock.send(query)
			response = self.sock.recv_json()
		except zmq.error.Again:
			raise RuntimeError("Server '%s' did not respond" % self.host)
		# TODO: Choose a better form of status codes
		if response['status'] == -404:
			raise KeyError
		elif response['status'] < 0:
			raise RuntimeError(response['info'])
		else:
			return response['data']
			
	def get_power_state(self):
		"""Returns 'on' or 'off'"""
		return self._ipmi_command("power status").split()[-1]
		
	def do_power(self, op='status'):
		return self._ipmi_command("power "+op)
		
	def _ipmi_command(self, cmd):
		username = self.config['ipmi']['username']
		password = self.config['ipmi']['password']
		#try:
		ret = subprocess.check_output(['ipmitool', '-H', self.host_ipmi,
		                                '-U', username, '-P', password] +
		                                cmd.split())
		return ret
		#return True
		#except CalledProcessError as e:
		#	raise RuntimeError(str(e))
		
	def stop_tbn(self):
		try:
			self._shell_command("stop adp-tbn")
			return True
		except subprocess.CalledProcessError:
			return False
			
	def start_tbn(self):
		try:
			self._shell_command("start adp-tbn")
			return True
		except subprocess.CalledProcessError:
			return False
			
	def restart_tbn(self):
		self.stop_tbn()
		return self.start_tbn()
		
	def status_tbn(self):
		try:
			return self._shell_command("status adp-tbn")
		except subprocess.CalledProcessError:
			return "unknown"
			
	def pid_tbn(self):
		try:
			pids = self._shell_command("ps aux | grep adp_tbn | grep -v grep | awk '{print $2}'")
			pids = pids.split('\n')[:-1]
			pids = [int(pid, 10) for pid in pids]
			if len(pids) == 0:
				pids = [-1,]
			return pids 
		except subprocess.CalledProcessError:
			return [-1,]
		except ValueError:
			return [-1,]
			
	def stop_tengine(self, tuning=0):
		try:
			self._shell_command("stop adp-tengine-%i" % tuning)
			return True
		except subprocess.CalledProcessError:
			return False
			
	def start_tengine(self, tuning=0):
		try:
			self._shell_command("start adp-tengine-%i" % tuning)
			return True
		except subprocess.CalledProcessError:
			return False
			
	def restart_tengine(self, tuning=0):
		self.stop_tengine(tuning=tuning)
		return self.start_tengine(tuning=tuning)
		
	def status_tengine(self, tuning=0):
		try:
			return self._shell_command("status adp-tengine-%i" % tuning)
		except subprocess.CalledProcessError:
			return "unknown"
			
	def pid_tengine(self, tuning=0):
		try:
			pids = self._shell_command("ps aux | grep adp_tengine | grep -- --tuning[=\ ]%i | grep -v grep | awk '{print $2}'" % tuning)
			pids = pids.split('\n')[:-1]
			pids = [int(pid, 10) for pid in pids]
			if len(pids) == 0:
				pids = [-1,]
			return pids 
		except subprocess.CalledProcessError:
			return [-1,]
		except ValueError:
			return [-1,]
			
	def stop_drx(self, tuning=0):
		try:
			self._shell_command("stop adp-drx-%i" % tuning)
			return True
		except subprocess.CalledProcessError:
			return False
		
	def start_drx(self, tuning=0):
		try:
			self._shell_command("start adp-drx-%i" % tuning)
			return True
		except subprocess.CalledProcessError:
			return False
			
	def restart_drx(self, tuning=0):
		self.stop_drx(tuning=tuning)
		return self.start_drx(tuning=tuning)
		
	def status_drx(self, tuning=0):
		try:
			return self._shell_command("status adp-drx-%i" % tuning)
		except subprocess.CalledProcessError:
			return "unknown"
			
	def pid_drx(self, tuning=0):
		try:
			pids = self._shell_command("ps aux | grep adp_drx | grep -- --tuning[=\ ]%i | grep -v grep | awk '{print $2}'"  % tuning)
			pids = pids.split('\n')[:-1]
			pids = [int(pid, 10) for pid in pids]
			if len(pids) == 0:
				pids = [-1,]
			return pids 
		except subprocess.CalledProcessError:
			return [-1,]
		except ValueError:
			return [-1,]
			
	def kill_pid(self, pid):
		try:
			self._shell_command("kill -9 %i" % pid)
			return True
		except subprocess.CalledProcessError:
			return False
			
	def _shell_command(self, cmd, timeout=5.):
		#return self.host
		# HACK TESTING
		#i = int(self.host[-1])
		#time.sleep(i)
		
		#self.log.info("Executing "+cmd+" on "+self.host)
		
		password = self.config['server']['password']
		#self.log.info("RUNNING SSHPASS " + cmd)
		ret = subprocess.check_output(['sshpass', '-p', password,
		                               'ssh', '-o', 'StrictHostKeyChecking=no',
		                               'root@'+self.host,
		                               cmd])
		#self.log.info("SSHPASS DONE: " + ret)
		#self.log.info("Command executed: "+ret)
		return ret
		#return True
		#import paramiko
		#''.decode('utf-8')  # dirty fix
		#py3compat.u("dirty hack")
		#ssh = paramiko.SSHClient()
		#ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
		#ssh.connect(self.host,
		#            username=self.config['server']['username'],
		#            password=self.config['server']['password'],
		#            timeout=timeout)
		#cmd += '; exit'
		#self.log.info("Executing "+cmd+" on "+self.host)
		#ssh_stdin, ssh_stdout, ssh_stderr = ssh.exec_command(cmd)
		#self.log.info("Closing SSH")
		#ssh_stdin.close()
		#ssh_stdout.close()
		#ssh_stderr.close()
		#ssh.close()
		#del ssh
		password = self.config['server']['password']
		#try:
		self.log.info("RUNNING SSHPASS " + cmd)
		ret = subprocess.check_output(['sshpass', '-p', password,
		                               'ssh', '-o', 'StrictHostKeyChecking=no',
		                               'root@'+self.host] +
		                              cmd.split())
		#ret = subprocess.check_output(['sshpass -p %s ssh root@%s %s' %
			#                               (password, self.host, cmd)],
			#                              shell=True)
		self.log.info("SSHPASS DONE: " + ret)
		#except subprocess.CalledProcessError:
		#	raise RuntimeError("Server command '%s' failed" % cmd)
		
	def can_ssh(self):
		try:
			#with lock:
			ret = self._shell_command('hostname')
			return True
			#except socket.error:
		#except RuntimeError:
		except subprocess.CalledProcessError:
			return False


# TODO: Rename this (and possibly refactor)
class Roach2MonitorClient(object):
	def __init__(self, config, log, num, syncFunction=None):
		# Note: num is 1-based index of the roach
		self.config = config
		self.log    = log
		self.roach  = AdpRoach(num, config['roach']['port'])
		self.host   = self.roach.hostname
		self.device = ROACH2Device(self.host)
		self.num = num
		self.syncFunction = syncFunction
		self.GBE_DRX_0 = 0
		self.GBE_DRX_1 = 1
		self.GBE_TBN = 2
		
	def unprogram(self, reboot=False):
		if not reboot:
			self.roach.unprogram()
			return
		# Note: paramiko must be pip installed (it's also included with fabric)
		#import paramiko # For ssh'ing into roaches to call reboot
		#ssh = paramiko.SSHClient()
		#ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
		#ssh.connect(self.host,
		#            username=self.config['roach']['username'],
		#            password=self.config['roach']['password'],
		#            timeout=5.)
		#ssh_stdin, ssh_stdout, ssh_stderr = ssh.exec_command('reboot')
		#ssh_stdout.read()
		## Note: This requires ssh authorized_keys to have been set up
		password = self.config['roach']['password']
		try:
			subprocess.check_output(['sshpass', '-p', password,
			                         'ssh', '-o', 'StrictHostKeyChecking=no',
			                         'root@'+self.host,
			                         'shutdown -r now'])
		except subprocess.CalledProcessError as e:
			try:
				# Note: The above shutdown command didn't always work,
				#         so this does a forced reboot.
				subprocess.check_output(['sshpass', '-p', password,
				                         'ssh', '-o', 'StrictHostKeyChecking=no',
				                         'root@'+self.host,
				                         '{ sleep 1; reboot -f; } >/dev/null &'])
			except subprocess.CalledProcessError as e:
				self.log.info("Failed to reboot roach %s: %s", self.host, str(e))
				raise RuntimeError("Roach reboot command failed")
				
	def get_samples(self, slot, stand, pol, nsamps=None):
		return self.get_samples_all(slot, nsamps)[stand,pol]
		
	@lru_cache_method(maxsize=4)
	def get_samples_all(self, slot, nsamps=None):
		"""Returns an NDArray of shape (stand,pol,sample)"""
		return self.device.samples_all(nsamps).transpose([1,2,0])
		
	@lru_cache_method(maxsize=4)
	def get_temperatures(self, slot):
		try:
			return self.device.temperatures()
		except:
			return {'error': float('nan')}
			
	def program(self):
		# Program with ADP firmware
		# Note: ADCs must be re-calibrated after doing this
		
		## Firmware
		boffile        = self.config['roach']['firmware']
		## Channel setup for each GbE interfaces
		nsubband0      = len(self.config['host']['servers-data'])
		subband_nchan0 = int(math.ceil(self.config['drx'][0]['capture_bandwidth'] / CHAN_BW / nsubband0))
		nsubband1      = len(self.config['host']['servers-data'])
		subband_nchan1 = int(math.ceil(self.config['drx'][1]['capture_bandwidth'] / CHAN_BW / nsubband1))
		nsubband2      = 1
		subband_nchan2 = int(math.ceil(self.config['tbn']['capture_bandwidth'] / CHAN_BW / nsubband2))
		## ADC digital gain
		adc_gain       = self.config['roach']['adc_gain']
		adc_gain_bits  = ( adc_gain       | (adc_gain <<  4) |
		                  (adc_gain << 8) | (adc_gain << 12) )
		adc_gain_reg   = 0x2a
		adc_registers  = {adc_gain_reg: adc_gain_bits}
		## Maximum number of attempts to try and program
		max_attempts   = self.config['roach']['max_program_attempts']
		## Whether or not to bypass the PFB on the FFT
		bypass_pfb     =  self.config['roach']['bypass_pfb']
		
		self.roach.program(boffile, nsubband0, subband_nchan0, nsubband1, subband_nchan1, nsubband2, subband_nchan2, 
					    adc_registers=adc_registers, max_attempts=max_attempts, bypass_pfb=bypass_pfb)
					    
	def configure_dual_mode(self):
		try:
			self.roach.stop_processing()
			# DRX, tuning 0 on gbe0, DRX, tuning 1 on gbe1, TBN on gbe2
			drx_dst_hosts   = self.config['host']['servers-data']
			tbn_dst_hosts   = [self.config['host']['servers-tbn'][self.num-1]]
			src_ip_base     = self.config['roach']['data_ip_base']
			src_port_base   = self.config['roach']['data_port_base']
			dst_ports       = self.config['server']['data_ports']
			drx_dst_ips     = [host2ip(host) for host in drx_dst_hosts]
			tbn_dst_ips     = [host2ip(host) for host in tbn_dst_hosts]
			macs = load_ethers()
			drx_dst_macs    = [macs[ip] for ip in drx_dst_ips]
			tbn_dst_macs    = [macs[ip] for ip in tbn_dst_ips]
			drx_arp_table   = gen_arp_table(drx_dst_ips, drx_dst_macs)
			tbn_arp_table   = gen_arp_table(tbn_dst_ips, tbn_dst_macs)
			drx_0_dst_ports = [dst_ports[0] for i in xrange(len(drx_dst_ips))]
			drx_1_dst_ports = [dst_ports[1] for i in xrange(len(drx_dst_ips))]
			tbn_dst_ports   = [dst_ports[2]] * len(tbn_dst_ips)
			ret0 = self.roach.configure_10gbe(self.GBE_DRX_0, drx_dst_ips, drx_0_dst_ports, drx_arp_table, src_ip_base, src_port_base)
			ret1 = self.roach.configure_10gbe(self.GBE_DRX_1, drx_dst_ips, drx_1_dst_ports, drx_arp_table, src_ip_base, src_port_base)
			ret2 = self.roach.configure_10gbe(self.GBE_TBN, tbn_dst_ips, tbn_dst_ports, tbn_arp_table, src_ip_base, src_port_base)
			if not ret0 or not ret1 or not ret2:
				raise RuntimeError("Configuring Roach 10GbE ports failed")
		except:
			self.log.exception("Configuring roach failed")
			raise
			
	@ISC.logException
	def configure_adc_delay(self, index, delay, relative=False):
		if relative:
			currDelay = self.roach.read_adc_delay(index)
			delay += currDelay
		self.roach.configure_adc_delay(index, delay)
		
	@ISC.logException
	def tune_drx(self, tuning, cfreq, shift_factor=27):
		bw = self.config['drx'][tuning]['capture_bandwidth']
		bw = round(bw, 3) # Round to mHz to avoid precision errors
		nsubband      = len(self.config['host']['servers-data'])
		subband_nchan = int(math.ceil(bw / CHAN_BW / nsubband))
		chan0         =  int(round(cfreq / CHAN_BW)) - nsubband*subband_nchan//2
		
		gbe = self.GBE_DRX_0 if tuning == 0 else self.GBE_DRX_1
		self.roach.configure_fengine(gbe, chan0, shift_factor=shift_factor)
		return chan0
		
	@ISC.logException
	def tune_tbn(self, cfreq, shift_factor=27):
		bw = self.config['tbn']['capture_bandwidth']
		bw = round(bw, 3) # Round to mHz to avoid precision errors
		nsubband = 1
		subband_nchan = int(math.ceil(bw / CHAN_BW / nsubband))
		chan0         =  int(round(cfreq / CHAN_BW)) - subband_nchan//2
		
		self.roach.configure_fengine(self.GBE_TBN, chan0, shift_factor=shift_factor)
		return chan0
		
	def start_processing(self):
		self.roach.start_processing(syncFunction=self.syncFunction)
		
	def stop_processing(self):
		self.roach.stop_processing()
		
	def processing_started(self):
		return self.roach.processing_started()
		
	def enable_drx_data(self, tuning):
		gbe = self.GBE_DRX_0 if tuning == 0 else self.GBE_DRX_1
		self.roach.enable_data(gbe)
		
	def enable_tbn_data(self):
		self.roach.enable_data(self.GBE_TBN)
		
	def disable_drx_data(self, tuning):
		gbe = self.GBE_DRX_0 if tuning == 0 else self.GBE_DRX_1
		self.roach.disable_data(gbe)
		
	def disable_tbn_data(self):
		self.roach.disable_data(self.GBE_TBN)
		
	def drx_data_enabled(self, tuning):
		gbe = self.GBE_DRX_0 if tuning == 0 else self.GBE_DRX_1
		return self.roach.data_enabled(gbe)
		
	def tbn_data_enabled(self):
		return self.roach.data_enabled(self.GBE_TBN)
		
	# TODO: Configure channel selection (based on FST)
	# TODO: start/stop data flow (remember to call roach.reset() before start)


def exception_in(vals, error_type=Exception):
	return any([isinstance(val, error_type) for val in vals])


class MsgProcessor(ConsumerThread):
	def __init__(self, config, log,
	             max_process_time=1.0, ncmd_save=4, dry_run=False):
		ConsumerThread.__init__(self)
		
		# HACK WAR for super nasty bug in Paramiko (threads during import)
		#import paramiko # For ssh'ing into roaches to call reboot
		
		self.config           = config
		self.log              = log
		self.shutdown_timeout = 3.
		self.dry_run          = dry_run
		self.msg_queue        = Queue()
		max_concurrent_msgs = int(MAX_MSGS_PER_SEC*max_process_time)
		self.thread_pool = ThreadPool(max_concurrent_msgs)
		self.name = "Adp.MsgProcessor"
		self.utc_start     = None
		self.utc_start_str = "NULL"
		
		self.messageServer = ISC.PipelineMessageServer(addr=('adp',5832))
		
		mcs_local_host  = self.config['mcs']['headnode']['local_host']
		mcs_local_port  = self.config['mcs']['headnode']['local_port']
		mcs_remote_host = self.config['mcs']['headnode']['remote_host']
		mcs_remote_port = self.config['mcs']['headnode']['remote_port']
		"""
		self.msg_receiver = MCS2.MsgReceiver((mcs_local_host, mcs_local_port),
		                                     subsystem=SUBSYSTEM)
		self.msg_sender   = MCS2.MsgSender((mcs_remote_host, mcs_remote_port),
		                                   subsystem=SUBSYSTEM)
		"""
		# Maps slot->[(cmd,ref,exit_code), ...]
		self.cmd_status = SequenceDict(list, maxlen=ncmd_save)
		#self.zmqctx = zmq.Context()
		
		self.headnode = ObjectPool([AdpServerMonitorClient(config, log, 'adp'),])
		self.servers = ObjectPool([AdpServerMonitorClient(config, log, host)
		                           for host in self.config['host']['servers']])
		#self.roaches = ObjectPool([Roach2MonitorClient(config, log, host)
		#                           for host in self.config['host']['roaches']])
		#nroach = len(self.config['host']['roaches'])
		nroach = NBOARD
		self.roaches = ObjectPool([Roach2MonitorClient(config, log, num+1)
		                           for num in xrange(nroach)])
		for arc in self.roaches:
			arc.syncFunction = self.roaches[0].roach.wait_for_pps
			
		#self.fst = Fst(config, log)
		self.drx = Drx(config, log, self.messageServer, self.servers, self.roaches)
		self.tbf = Tbf(config, log, self.messageServer, self.servers, self.roaches)
		self.bam = Bam(config, log, self.messageServer, self.servers, self.roaches)
		self.cor = Cor(config, log, self.messageServer, self.servers, self.roaches)
		self.tbn = Tbn(config, log, self.messageServer, self.servers, self.roaches)

		self.serial_number = '1'
		self.version = str(__version__)
		self.state = {}
		self.state['status']  = 'SHUTDWN'
		self.state['info']    = 'Need to INI ADP'
		self.state['lastlog'] = ('Welcome to ADP S/N %s, version %s' %
		                         (self.serial_number, self.version))
		self.state['activeProcess'] = []
		
		self.shutdown_event = threading.Event()
		
		self.run_execute_thread = threading.Thread(target=self.run_execute)
		self.run_execute_thread.daemon = True
		self.run_execute_thread.start()
		
		# TODO: This causes INI to lock up on a call to self.roaches.host!
		#self.run_monitor_thread = threading.Thread(target=self.run_monitor)
		#self.run_monitor_thread.daemon = True
		#self.run_monitor_thread.start()
		
		self.run_failsafe_thread = threading.Thread(target=self.run_failsafe)
		self.run_failsafe_thread.daemon = True
		self.run_failsafe_thread.start()
		
		self.start_synchronizer_thread()
		self.start_lock_thread()
		
	def start_synchronizer_thread(self):
		self.tbn_sync_server = MCS2.SynchronizerServer()
		self.run_synchronizer_thread = threading.Thread(target=self.tbn_sync_server.run)
		self.run_synchronizer_thread.start()
		
	def stop_synchronizer_thread(self):
		self.tbn_sync_server.shutdown()
		self.run_synchronizer_thread.join()
		del self.tbn_sync_server
		
	@ISC.logException
	def start_lock_thread(self):
		self.lock_server = ISC.PipelineEventServer(addr=('adp',5834), timeout=300)
		self.lock_server.start()
		
	@ISC.logException
	def stop_lock_thread(self):
		try:
			self.lock_server.stop()
			del self.lock_server
		except AttributeError:
			pass
			
	def uptime(self):
		# Returns no. secs since data processing began (during INI)
		if self.utc_start is None:
			return 0
		secs = (datetime.datetime.utcnow() - self.utc_start).total_seconds()
		return secs
		
	def raise_error_state(self, cmd, state):
		# TODO: Need new codes? Need to document them?
		state_map = {'BOARD_SHUTDOWN_FAILED':      (0x08,'Board-level shutdown failed'),
		             'BOARD_PROGRAMMING_FAILED':   (0x04,'Board programming failed'),
		             'BOARD_CONFIGURATION_FAILED': (0x05,'Board configuration failed'),
		             'SERVER_STARTUP_FAILED':      (0x09,'Server startup failed'),
		             'SERVER_SHUTDOWN_FAILED':     (0x0A,'Server shutdown failed'), 
		             'PIPELINE_STARTUP_FAILED':    (0x0B,'Pipeline startup failed'),
		             'ADC_CALIBRATION_FAILED':     (0x0C,'ADC offset calibration failed')}
		code, msg = state_map[state]
		self.state['lastlog'] = '%s: Finished with error' % cmd
		self.state['status']  = 'ERROR'
		self.state['info']    = 'SUMMARY! 0x%02X! %s' % (code, msg)
		self.state['activeProcess'].pop()
		return code
		
	def check_success(self, func, description, names):
		self.state['info'] = description
		rets = func()
		#self.log.info("check_success rets: "+' '.join([str(r) for r in rets]))
		oks = [True for _ in rets]
		for i, (name, ret) in enumerate(zip(names, rets)):
			if isinstance(ret, Exception):
				oks[i] = False
				self.log.error("%s: %s" % (name, str(ret)))
		all_ok = all(oks)
		if not all_ok:
			symbols = ''.join(['.' if ok else 'x' for ok in oks])
			self.log.error("%s failed: %s" % (description, symbols))
			self.state['info'] = description + " failed"
		else:
			self.state['info'] = description + " succeeded"
		return all_ok
		
	def ini(self, arg=None):
		start_time = time.time()
		# Note: Return value from this function is not used
		self.state['activeProcess'].append('INI')
		self.state['status'] = 'BOOTING'
		self.state['info']   = 'Running INI sequence'
		self.log.info("Running INI sequence")
		
		# Figure out if the servers are up or not
		if not all(self.servers.can_ssh()):
			## Down, power them on
			self.log.info("Powering on servers")
			if not self.check_success(lambda: self.servers.do_power('on'),
			                          'Powering on servers',
			                          self.servers.host):
				if 'FORCE' not in arg:
					return self.raise_error_state('INI', 'SERVER_STARTUP_FAILED')
			startup_timeout = self.config['server']['startup_timeout']
			try:
				#self._wait_until_servers_power('on', startup_timeout)
				# **TODO: Use this instead when Paramiko issues resolved!
				self._wait_until_servers_can_ssh(    startup_timeout)
			except RuntimeError:
				if 'FORCE' not in arg:
					return self.raise_error_state('INI', 'SERVER_STARTUP_FAILED')
					
		## Stop the pipelines
		self.log.info('Stopping pipelines')
		for tuning in xrange(2):
			self.servers.stop_drx(tuning=tuning)
			self.headnode.stop_tengine(tuning=tuning)
		self.servers.stop_tbn()
		
		## Make sure the pipelines have stopped
		try:
			self._wait_until_pipelines_stopped(max_wait=40)
		except RuntimeError:
			self.log.warning('Some pipelines have failed to stop, trying harder')
			for tuning in xrange(2):
				for server in self.headnode:
					pids = server.pid_tengine(tuning=tuning)
					for pid in filter(lambda x: x > 0, pids):
						self.log.warning('  Killing %s TEngine-%i, PID %i', server.host, tuning, pid)
						server.kill_pid(pid)
				for server in self.servers:
					pids = server.pid_drx(tuning=tuning)
					for pid in filter(lambda x: x > 0, pids):
						self.log.warning('  Killing %s DRX-%i, PID %i', server.host, tuning, pid)
						server.kill_pid(pid)
			for server in self.servers:
				pids = server.pid_tbn()
				for pid in filter(lambda x: x > 0, pids):
					self.log.warning('  Killing %s TBN, PID %i', server.host, pid)
					server.kill_pid(pid)
					
		self.log.info("Forcing CPUs into performance mode")
		self.headnode._shell_command('/root/fixCPU.sh')
		self.servers._shell_command('/root/fixCPU.sh')
		
		# WAR for synchronizer getting into a bad state when clients are killed
		self.log.info("Stopping Synchronizer thread")
		self.stop_synchronizer_thread()
		time.sleep(3)
		# WAR for synchronizer getting into a bad state when clients are killed
		self.log.info("Starting Synchronizer thread")
		self.start_synchronizer_thread()
		
		self.log.info("Stopping Lock thread")
		self.stop_lock_thread()
		time.sleep(3)
		self.log.info("Starting Lock thread")
		self.start_lock_thread()
		
		# Note: Must do this to ensure pipelines wait for the new UTC_START
		self.utc_start     = None
		self.utc_start_str = 'NULL'
		
		# Bring up the pipelines
		can_ssh_status = ''.join(['.' if ok else 'x' for ok in self.servers.can_ssh()])
		self.log.info("Can ssh: "+can_ssh_status)
		if all(self.servers.can_ssh()) or 'FORCE' in arg:
			self.log.info("Restarting pipelines")
			for tuning in xrange(len(self.config['drx'])):
				if not self.check_success(lambda: self.headnode.restart_tengine(tuning=tuning),
									 'Restarting pipelines - DRX/T-engine',
									 self.headnode.host):
					if 'FORCE' not in arg:
						return self.raise_error_state('INI', 'SERVER_STARTUP_FAILED')
				if not self.check_success(lambda: self.servers.restart_drx(tuning=tuning),
									 'Restarting pipelines - DRX',
									 self.servers.host):
					if 'FORCE' not in arg:
						return self.raise_error_state('INI', 'SERVER_STARTUP_FAILED')
			if not self.check_success(lambda: self.servers.restart_tbn(),
			                          'Restarting pipelines - TBN',
			                          self.servers.host):
				if 'FORCE' not in arg:
					return self.raise_error_state('INI', 'SERVER_STARTUP_FAILED')
					
		# Bring up the FPGAs
		if 'NOREPROGRAM' not in arg: # Note: This is for debugging, not in spec
			self.log.info("Programming FPGAs")
			if not self.check_success(lambda: self.roaches.program(),
			                          'Programming FPGAs',
			                          self.roaches.host):
				if 'FORCE' not in arg: # Note: Also not in spec
					return self.raise_error_state('INI', 'BOARD_PROGRAMMING_FAILED')
					
		self.log.info("Configuring FPGAs")
		if not self.check_success(lambda: self.roaches.configure_dual_mode(),
		                          'Configuring FPGAs',
		                          self.roaches.host):
			if 'FORCE' not in arg:
				return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
				
		self.log.info("  Finished configuring FPGAs")
		
		start_delay = 5.
		utc_now   = datetime.datetime.utcnow()
		utc_start = utc_now + datetime.timedelta(0, start_delay)
		utc_init  = utc_start - datetime.timedelta(0, 1) # 1 sec before
		utc_init_str  = utc_start.strftime(DATE_FORMAT)
		utc_start = utc_start + datetime.timedelta(0, 2) # 2 sec after
		utc_start_str = utc_start.strftime(DATE_FORMAT)
		self.utc_start     = utc_start
		self.utc_start_str = utc_start_str
		self.state['lastlog'] = "Starting processing at UTC "+utc_start_str
		self.log.info("Starting processing at UTC "+utc_start_str)
		
		# TODO: Tell server pipelines the value of utc_start_str and have them
		#         await imminent data.
		
		# Wait until we're in the middle of the init sec
		wait_until_utc_sec(utc_init_str)
		time.sleep(0.5)
		self.state['lastlog'] = "Starting processing now"
		self.log.info("Starting FPGA processing now")
		if not self.check_success(lambda: self.roaches.start_processing(),
		                          'Starting FPGA processing',
		                          self.roaches.host):
			if 'FORCE' not in arg:
				return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
		time.sleep(0.1)
		self.log.info("Checking FPGA processing")
		if not all(self.roaches.processing_started()):
			if 'FORCE' not in arg:
				return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
				
		# Check for a time skew on the roach boards and the start time
		self.roaches[0].roach.wait_for_pps()
		self.roaches[0].roach.wait_for_pps()
		markS = time.time()
		markP = self.roaches[0].roach.fpga.read_uint('pkt_gbe0_eof_cnt')
		markN = self.roaches[0].roach.fpga.read_uint('pkt_gbe0_n_subband')
		markR = self.roaches[0].roach.fpga.read_uint('adc_sync_count')
		self.log.info("Server: %i + %.6f", int(markS), markS-int(markS))
		self.log.info("Roach: %i + %.6f", markR, 1.0*(markP/markN)/CHAN_BW)
		utc_start     = datetime.datetime.utcfromtimestamp(int(markS) - markR) + datetime.timedelta(seconds=1)
		utc_start_str = utc_start.strftime(DATE_FORMAT)
		self.state['lastlog'] = "Processing started at UTC "+utc_start_str
		self.log.info("Processing started at UTC "+utc_start_str)
		if utc_start_str != self.utc_start_str:
			self.log.error("Processing start time mis-match")
			return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
			
		# Check and make sure that *all* of the pipelines started
		self.log.info("Checking pipeline processing")
		## TBN
		pipeline_pids = [p for s in self.servers.pid_tbn() for p in s]
		pipeline_pids = filter(lambda x: x>0, pipeline_pids)
		print 'TBN:', len(pipeline_pids), pipeline_pids
		if len(pipeline_pids) != len(self.servers):
			self.log.error('Found %i TBN pipelines running, expected %i', len(pipeline_pids), len(self.servers))
			if 'FORCE' not in arg:
				return self.raise_error_state('INI', 'PIPELINE_STARTUP_FAILED')
		## DRX
		pipeline_pids = []
		for tuning in xrange(len(self.config['drx'])):
			pipeline_pids = [p for s in self.servers.pid_drx(tuning=tuning) for p in s]
			pipeline_pids = filter(lambda x: x>0, pipeline_pids)
			print 'DRX-%i:' % tuning, len(pipeline_pids), pipeline_pids
			if len(pipeline_pids) != len(self.servers):
				self.log.error('Found %i DRX-%i pipelines running, expected %i', len(pipeline_pids), tuning, len(self.servers))
				if 'FORCE' not in arg:
					return self.raise_error_state('INI', 'PIPELINE_STARTUP_FAILED')
		## T-engine
		for tuning in xrange(len(self.config['drx'])):
			pipeline_pids = [p for s in self.headnode.pid_tengine(tuning=tuning) for p in s]
			pipeline_pids = filter(lambda x: x>0, pipeline_pids)
			print 'TEngine-%i:' % tuning, len(pipeline_pids), pipeline_pids
			if len(pipeline_pids) != 1:
				self.log.error('Found %i TEngine-%i pipelines running, expected %i', len(pipeline_pids), tuning,  1)
				if 'FORCE' not in arg:
					return self.raise_error_state('INI', 'PIPELINE_STARTUP_FAILED')
		self.log.info('Checking pipeline processing succeeded')
		
		#self.log.info("Initializing TBN")
		#if not self.check_success(lambda: self.tbn.tune(),
		#                         'Initializing TBN',
		#                         self.roaches.host):
		#	if 'FORCE' not in arg:
		#		return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
		self.log.info("Initializing DRX")
		self.roaches[0].roach.wait_for_pps()
		for tuning in xrange(len(self.config['drx'])):
			if not self.check_success(lambda: self.drx.tune(tuning=tuning, freq=20e6),
			                          'Initializing DRX - %i' % tuning,
			                          self.roaches.host):
				if 'FORCE' not in arg:
					return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
		for tuning in xrange(len(self.config['drx'])):
			if not all(self.roaches.drx_data_enabled(tuning)):
				return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
		#if not all(self.roaches.tbn_data_enabled()):
		#	return self.raise_error_state('INI', 'BOARD_CONFIGURATION_FAILED')
		time.sleep(0.1)
		
		# Calibration
		if 'NOREPROGRAM' not in arg: # Note: This is for debugging, not in spec
			if not self.adc_cal(arg):
				if 'FORCE' not in arg: # Note: Also not in spec
					return self.raise_error_state('INI', 'ADC_CALIBRATION_FAILED')
					
		self.log.info("INI finished in %.3f s", time.time() - start_time)
		self.state['lastlog'] = 'INI finished in %.3f s' % (time.time() - start_time)
		self.state['status']  = 'NORMAL'
		self.state['info'] = 'System calibrated and operating normally'
		self.state['activeProcess'].pop()
		return 0
		
	def adc_cal(self, arg=None):
		"""
		ADC offset calibration routine.
		"""
		
		status = True
		self.log.info("Starting ADC offset calibration")
		
		# Helper function - download files
		def _downloadFiles(tTrigger, nTunings=len(self.config['drx']), ageLimit=10.0, deleteAfterCopy=False):
			filenames = []
			for s in (1,2,3,4,5,6):
				cmd = "ssh adp%i 'ls -lt --time-style=\"+%%s\" /data0/test_adp*_*.tbf' | head -n%i " % (s, nTunings)
				latestTBF = subprocess.check_output(cmd, shell=True)
				lines = latestTBF.split('\n')
				for f,line in enumerate(lines):
					if len(line) < 3:
						continue
						
					try:
						fields = line.split()
						mtime, filename = float(fields[5]), fields[6]
						print '!!', tTrigger, mtime, tTrigger - mtime
						if abs(tTrigger - mtime) < ageLimit:
							outname = '/data0/adc_cal_%i_%i' % (s, f+1)
							subprocess.check_output("scp adp%i:%s %s" % (s, filename, outname), shell=True)
							filenames.append( outname )
							
							if deleteAfterCopy:
								subprocess.check_output("ssh adp%i 'rm -f %s'" % (s, filename), shell=True)
								
					except Exception as e:
						self.log.info("ERROR: line %i - %s", f+1, str(e))

			return filenames
			
		# Helper function - delete files
		def _deleteFiles(filenames):
			status = True
			for filename in filenames:
				try:
					os.unlink(filename)
				except OSError:
					status = False
			return True
		
		# Tune to 30 MHz
		freq = 30.1e6
		shift_factor = 24
		self.check_success(lambda: self.roaches.tune_drx(0, freq, shift_factor=shift_factor),
		                   'Tuning DRX to the calibration tone',
		                    self.roaches.host)
		time.sleep(20.0)
		
		# Run a TBF capture and save to disk
		self.log.info("Triggering local TBF dump")
		self.messageServer.trigger(0, int(0.06*FS), 1, local=True)
		tTrigger = time.time()
		time.sleep(5.0)
		
		# Analyze
		self.log.info("Analyzing TBF capture")
		filenames = _downloadFiles(tTrigger)
				
		if len(filenames) >= 6 or 'FORCE' in arg:
			# Solve for the delays
			output = subprocess.check_output("python /home/adp/lwa_sv/scripts/calibrateADCDelays.py %s" % ' '.join(filenames), shell=True)
			
			# Load in the delays
			output = output.split('\n')
			delays = {}
			for line in output:
				if line.find('Peak Power') != -1:
					self.log.info('Initial - %s', line)
					
				#self.log.info('Log line - initial: %s', line.rstrip())
				if line.startswith('roach'):
					r, i, d = line.split(None, 2)
					r = int(r[5:], 10) - 1
					i = int(i[5:], 10) - 1
					d = int(d, 10)
					##d = max([-1, min([1, d])])	# Constrain to -1, 0, 1
					try:
						delays[r][i] = d
					except KeyError:
						delays[r] = [0 for j in xrange(32)]
						delays[r][i] = d
						
			# Update the roach delay FIFOs
			self.log.info("Setting roach FIFO delays")
			badCount = 0
			for r in delays.keys():
				for i,d in enumerate(delays[r]):
					try:
						self.log.debug('Setting ADC delay for roach %i, input %i to %i', r+1, i+1, d)
						self.roaches[r].configure_adc_delay(i, -d, relative=True)
					except Exception as e:
						badCount += 1
						self.log.error('Error: %s', str(e))
						
			time.sleep(20.0)
			
			# Cleanup
			_deleteFiles(filenames)
			
			# Check by running another TBF capture
			self.log.info("Triggering local TBF dump")
			self.messageServer.trigger(0, int(0.06*FS), 1, local=True)
			tTrigger = time.time()
			time.sleep(5.0)
			
			# Analyze
			self.log.info("Verifying TBF capture")
			filenames = _downloadFiles(tTrigger)
			if len(filenames) >= 6 or 'FORCE' in arg:
				# Verify the delays
				output = subprocess.check_output("python /home/adp/lwa_sv/scripts/calibrateADCDelays.py %s" % ' '.join(filenames), shell=True)
				
				# Load in the delays
				output = output.split('\n')
				delays = []
				for line in output:
					if line.find('Peak Power') != -1:
						self.log.info('Final - %s', line)
						
					#self.log.info('Log line - final: %s', line.rstrip())
					if line.startswith('roach'):
						_, _, delay = line.split(None, 2)
						delays.append( int(delay, 10) )
						
				# Check
				nZero = 0
				for delay in delays:
					if delay == 0:
						nZero += 1
				self.log.info('After calibration there are %i inputs at zero', nZero)
				nZero += badCount
				self.log.info('After calibration and badCount there are %i inputs at zero', nZero)
				if nZero >= 487:
					status &= True
				else:
					status &= False
					
			else:
				self.log.warning("adc_cal second pass - downloaded only %i files", len(filenames))
				for filename in filenames:
					self.log.warning("  %s", filename)
				status = False
		else:
			self.log.warning("adc_cal first pass - downloaded only %i files", len(filenames))
			for filename in filenames:
				self.log.warning("  %s", filename)
			status = False
			
		# Cleanup
		_deleteFiles(filenames)
		
		# Reset the FFT shift factor
		self.check_success(lambda: self.roaches.tune_drx(0, freq),
		                   'Resetting FFT shift factor to default',
		                    self.roaches.host)
		time.sleep(1.0)
		
		# Final report
		if status:
			self.log.info('Calibration succeeded')
		else:
			self.log.error('Calibration failed')
			
		# Done
		return status
		
	def sht(self, arg=''):
		# TODO: Consider allowing specification of 'only servers' or 'only boards'
		start_time = time.time()
		self.state['activeProcess'].append('SHT')
		self.state['status'] = 'SHUTDWN'
		# TODO: Use self.check_success here like in ini()
		self.log.info("System is shutting down")
		self.state['info']   = 'System is shutting down'
		do_reboot = ('HARD' in arg)
		if 'SCRAM' in arg:
			if 'RESTART' in arg:
				if exception_in(self.servers.do_power('reset')):
					if 'FORCE' not in arg:
						return self.raise_error_state('SHT', 'SERVER_SHUTDOWN_FAILED')
				if exception_in(self.roaches.unprogram(do_reboot)):
					if 'FORCE' not in arg:
						return self.raise_error_state('SHT', 'BOARD_SHUTDOWN_FAILED')
			else:
				if exception_in(self.servers.do_power('off')):
					if 'FORCE' not in arg:
						return self.raise_error_state('SHT', 'SERVER_SHUTDOWN_FAILED')
				if exception_in(self.roaches.unprogram(do_reboot)):
					if 'FORCE' not in arg:
						return self.raise_error_state('SHT', 'BOARD_SHUTDOWN_FAILED')
			self.log.info("SHT SCRAM finished in %.3f s", time.time() - start_time)
			self.state['lastlog'] = 'SHT finished in %.3f s' % (time.time() - start_time)
			self.state['status']  = 'SHUTDWN'
			self.state['info']    = 'System has been shut down'
			self.state['activeProcess'].pop()
		else:
			if 'RESTART' in arg:
				def soft_reboot():
					self.log.info('Shutting down servers')
					if exception_in(self.servers.do_power('soft')):
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'SERVER_SHUTDOWN_FAILED')
					self.log.info('Unprogramming roaches')
					if exception_in(self.roaches.unprogram(do_reboot)):
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'BOARD_SHUTDOWN_FAILED')
					self.log.info('Waiting for servers to power off')
					try:
						self._wait_until_servers_power('off')
					except RuntimeError:
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'SERVER_SHUTDOWN_FAILED')
					self.log.info('Powering on servers')
					if exception_in(self.servers.do_power('on')):
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'SERVER_STARTUP_FAILED')
					self.log.info('Waiting for servers to power on')
					try:
						self._wait_until_servers_power('on')
					except RuntimeError:
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'SERVER_STARTUP_FAILED')
					self.log.info("SHT RESTART finished in %.3f s", time.time() - start_time)
					self.state['lastlog'] = 'SHT finished in %.3f s' % (time.time() - start_time)
					self.state['status']  = 'SHUTDWN'
					self.state['info']    = 'System has been shut down'
					self.state['activeProcess'].pop()
				self.thread_pool.add_task(soft_reboot)
			else:
				def soft_power_off():
					self.log.info('Shutting down servers')
					if exception_in(self.servers.do_power('soft')):
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'SERVER_SHUTDOWN_FAILED')
					self.log.info('Unprogramming roaches')
					if exception_in(self.roaches.unprogram(do_reboot)):
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'BOARD_SHUTDOWN_FAILED')
					self.log.info('Waiting for servers to power off')
					try:
						self._wait_until_servers_power('off')
					except RuntimeError:
						if 'FORCE' not in arg:
							return self.raise_error_state('SHT', 'SERVER_SHUTDOWN_FAILED')
					self.log.info("SHT finished in %.3f s", time.time() - start_time)
					self.state['lastlog'] = 'SHT finished in %.3f s' % (time.time() - start_time)
					self.state['status']  = 'SHUTDWN'
					self.state['info']    = 'System has been shut down'
					self.state['activeProcess'].pop()
				self.thread_pool.add_task(soft_power_off)
		return 0
		
	def _wait_until_servers_power(self, target_state, max_wait=30):
		# TODO: Need to check for ping (or even ssh connectivity) instead of 'power is on'?
		time.sleep(6)
		wait_time = 6
		while not all( (state == target_state
		                for state in self.servers.get_power_state()) ):
			time.sleep(2)
			wait_time += 2
			if wait_time >= max_wait:
				raise RuntimeError("Timed out waiting for server(s) to turn "+target_state)
				
	def _wait_until_servers_can_ssh(self, max_wait=60):
		wait_time = 0
		while not all(self.servers.can_ssh()):
			time.sleep(2)
			wait_time += 2
			if wait_time >= max_wait:
				raise RuntimeError("Timed out waiting to ssh to server(s)")
				
	def _wait_until_pipelines_stopped(self, max_wait=60):
		nRunning = 1
		t0, t1 = time.time(), time.time()
		while nRunning > 0:
			pids = []
			for tuning in xrange(2):
				for server in self.servers:
					pids.extend( server.pid_drx(tuning=tuning) )
				for server in self.headnode:
					pids.extend( server.pid_tengine(tuning=tuning) )
			for server in self.servers:
				pids.extend( server.pid_tbn() )
			nRunning = len( filter(lambda x: x > 0, pids) )
			
			t1 = time.time()
			if t1-t0 >= max_wait:
				raise RuntimeError("Timed out waiting for pipelines to stop")
			time.sleep(5)
			
	def run_execute(self):
		self.log.info("Starting slot execution thread")
		slot = MCS2.get_current_slot()
		while not self.shutdown_event.is_set():
			for cmd_processor in [self.drx, self.tbf, self.bam, self.cor, self.tbn]:#, self.fst, self.bam]
				self.thread_pool.add_task(cmd_processor.execute_commands,
				                          slot)
			while MCS2.get_current_slot() == slot:
				time.sleep(0.1)
			time.sleep(0.1)
			slot += 1
			
	def run_monitor(self):
		self.log.info("Starting monitor thread")
		while not self.shutdown_event.is_set():
			"""
			roach_drx_link_status = self.roaches.roach.check_link(0)
			roach_tbn_link_status = self.roaches.roach.check_link(1)
			drx_link_str = ''.join([('x','.')[bool(stat)] for stat in roach_drx_link_status])
			tbn_link_str = ''.join([('x','.')[bool(stat)] for stat in roach_tbn_link_status])
			if not all(roach_drx_link_status):
				self.log.warning("DRX gbe link failure: " + drx_link_str)
			if not all(roach_tbn_link_status):
				self.log.warning("TBN gbe link failure: " + tbn_link_str)
			"""
			roach_overflow_status = self.roaches.roach.check_overflow()
			drx_overflow = [stat[0] for stat in roach_overflow_status]
			tbn_overflow = [stat[1] for stat in roach_overflow_status]
			drx_overflow_str = ''.join([('.','x')[bool(stat)] for stat in drx_overflow])
			tbn_overflow_str = ''.join([('.','x')[bool(stat)] for stat in tbn_overflow])
			if any(drx_overflow):
				self.log.warning("DRX fifo overflow: " + drx_overflow_str)
			if any(tbn_overflow):
				self.log.warning("TBN fifo overflow: " + tbn_overflow_str)
			
			time.sleep(30)
			
	def run_failsafe(self):
		self.log.info("Starting failsafe thread")
		while not self.shutdown_event.is_set():
			slot = MCS2.get_current_slot()
			# Note: Actually just flattening lists, not summing
			roach_temps  = sum(self.roaches.get_temperatures(slot).values(), [])
			server_temps = sum(self.servers.get_temperatures(slot).values(), [])
			# Remove error values before reducing
			roach_temps  = [val for val in roach_temps  if not math.isnan(val)]
			server_temps = [val for val in server_temps if not math.isnan(val)]
			if len(roach_temps) == 0: # If all values were nan (exceptional!)
				roach_temps = [float('nan')]
			if len(server_temps) == 0: # If all values were nan (exceptional!)
				server_temps = [float('nan')]
			roach_temps_max  = np.max(roach_temps)
			server_temps_max = np.max(server_temps)
			if roach_temps_max > self.config['roach']['temperature_shutdown']:
				self.state['lastlog'] = 'Temperature shutdown (BOARD)' % cmd
				self.state['status']  = 'ERROR'
				self.state['info']    = '%s! 0x%02X! %s' % ('BOARD_TEMP_MAX', 0x01,
				                                            'Board temperature shutdown')
				if roach_temps_max > self.config['roach']['temperature_scram']:
					self.sht('SCRAM')
				else:
					self.sht()
			elif roach_temps_max > self.config['roach']['temperature_warning']:
				self.state['lastlog'] = 'Temperature warning (BOARD)' % cmd
				self.state['status']  = 'WARNING'
				self.state['info']    = '%s! 0x%02X! %s' % ('BOARD_TEMP_MAX', 0x01,
				                                            'Board temperature warning')
			if server_temps_max > self.config['server']['temperature_shutdown']:
				self.state['lastlog'] = 'Temperature shutdown (SERVER)' % cmd
				self.state['status']  = 'ERROR'
				self.state['info']    = '%s! 0x%02X! %s' % ('SERVER_TEMP_MAX', 0x01,
				                                            'Server temperature shutdown')
				if server_temps_max > self.config['server']['temperature_scram']:
					self.sht('SCRAM')
				else:
					self.sht()
			elif server_temps_max > self.config['server']['temperature_warning']:
				self.state['lastlog'] = 'Temperature warning (SERVER)' % cmd
				self.state['status']  = 'WARNING'
				self.state['info']    = '%s! 0x%02X! %s' % ('SERVER_TEMP_MAX', 0x01,
				                                            'Server temperature warning')
			self.log.info("Failsafe OK")
			time.sleep(self.config['failsafe_interval'])
			
	#def run_synchronizer(self):
	#	self.log.info("Starting Synchronizer server")
	#	self.sync_server = MCS2.SynchronizerServer()
	#	self.sync_server.run()
		
	def process(self, msg):
		if msg.cmd == 'PNG':
			self.log.info('Received PNG: '+str(msg))
			if not self.dry_run:
				self.process_msg(msg, lambda msg: True, '')
		elif msg.cmd == 'RPT':
			if msg.data != 'UTC_START':
				self.log.info('Received RPT request: '+str(msg))
			if not self.dry_run:
				# Note: RPT messages are processed asynchronously
				#         to avoid stalls.
				# TODO: Check that this doesn't cause any problems
				#         due to race conditions.
				self.thread_pool.add_task(self.process_msg,
				                          msg, self.process_report)
		else:
			self.log.info('Received command: '+str(msg))
			if not self.dry_run:
				self.process_msg(msg, self.process_command)
		"""
		next_slot = MCS2.get_current_slot() + 1
		# TODO: Could defer replies until here for better error handling
		for cmd_processor in [self.drx, self.tbn]:#, self.fst, self.bam]
			self.thread_pool.add_task(cmd_processor.execute_commands,
			                          next_slot)
		#self.drx.execute_commands(next_slot)
		#*self.fst.execute_commands(next_slot)
		#self.bam.execute_commands(next_slot)
		"""
		
	def shutdown(self):
		self.shutdown_event.set()
		self.stop_synchronizer_thread()
		self.stop_lock_thread()
		# Propagate shutdown to downstream consumers
		self.msg_queue.put(ConsumerThread.STOP)
		if not self.thread_pool.wait(self.shutdown_timeout):
			self.log.warning("Active tasks still exist and will be killed")
		self.run_execute_thread.join(self.shutdown_timeout)
		if self.run_execute_thread.isAlive():
			self.log.warning("run_execute thread still exists and will be killed")
		#self.run_monitor_thread.join(self.shutdown_timeout)
		#if self.run_monitor_thread.isAlive():
		#	self.log.warning("run_monitor thread still exists and will be killed")
		print self.name, "shutdown"
		
	def process_msg(self, msg, process_func):
		accept, reply_data = process_func(msg)
		status = self.state['status']
		reply_msg = msg.create_reply(accept, status, reply_data)
		self.msg_queue.put(reply_msg)
		
	def process_report(self, msg):
		key, args = MCS2.mib_parse_label(msg.data)
		try: value = self._get_report_result(key, args, msg.slot)
		except KeyError:
			self.log.warning('Unknown MIB entry: %s' % msg.data)
			return False, 'Unknown MIB entry: %s' % msg.data
		except ValueError as e:
			self.log.warning(e)
			return False, str(e)
		#except (ValueError,RuntimeError) as e:
		except Exception as e:
			self.log.error('%s: %s'%(type(e), str(e)))
			return False, '%s: %s'%(type(e), str(e))
		reply_data = self._pack_report_result(key, value)
		log_data   = self._format_report_result(key, value)
		self.log.debug('%s = %s' % (msg.data, log_data))
		return True, reply_data
		
	def _get_next_fir_index(self):
		idx = self.fir_idx
		self.fir_idx += 1
		self.fir_idx %= NINPUT
		return idx
		
	def _get_report_result(self, key, args, slot):
		reduce_ops = {'MAX':      np.max,
		              'MIN':      np.min,
		              'AVG':      np.mean,
		              'RMS':      lambda x: np.sqrt(np.mean(x**2)),
		              'SAT':      lambda x: np.sum(np.abs(x)>=ADC_MAXVAL),
		              'DCOFFSET': np.mean,
		              'PEAK':     np.max}
		if key == 'SUMMARY':         return self.state['status']
		if key == 'INFO':            return self.state['info']
		if key == 'LASTLOG':         return self.state['lastlog']
		if key == 'SUBSYSTEM':       return SUBSYSTEM
		if key == 'SERIALNO':        return self.serial_number
		if key == 'VERSION':         return self.version
		# TODO: TBF_STATUS
		#       TBF_TUNING_MASK
		if key == 'NUM_STANDS':      return NSTAND
		if key == 'NUM_SERVERS':     return NSERVER
		if key == 'NUM_BOARDS':      return NBOARD
		if key == 'NUM_TBN_BITS':    return TBN_BITS
		if key == 'TBN_CONFIG_FREQ': return self.tbn.cur_freq
		if key == 'TBN_CONFIG_FILTER': return self.tbn.cur_filt
		if key == 'TBN_CONFIG_GAIN': return self.tbn.cur_gain
		# TODO: NUM_BEAMS
		if key == 'BEAM_FIR_COEFFS': return FIR_NCOEF
		# TODO: T_NOM
		if key == 'NUM_DRX_TUNINGS': return self.drx.ntuning
		if args[0] == 'DRX' and args[1] == 'CONFIG':
			tuning = args[2]-1
			if args[3] == 'FREQ':
				return self.drx.cur_freq[tuning]
			if args[3] == 'FILTER':
				return self.drx.cur_filt[tuning]
			if args[3] == 'GAIN':
				return self.drx.cur_gain[tuning]
		if key == 'NUM_FREQ_CHANS':  return NCHAN
		if key == 'FIR_CHAN_INDEX':  return self._get_next_fir_index()
		if key == 'FIR':
			return self.fst.get_fir_coefs(slot)[input2standpol(self.fir_idx)]
		if key == 'CLK_VAL':         return MCS2.slot2mpm(slot-1)
		if key == 'UTC_START':       return self.utc_start_str # Not in spec
		if key == 'UPTIME':          return self.uptime() # Not in spec
		if key == 'STAT_SAMP_SIZE':  return STAT_SAMP_SIZE
		if args[0] == 'ANT':
			inp = args[1]-1
			if not (0 <= inp < NINPUT):
				raise ValueError("Unknown input number %i"%(inp+1))
			board,stand,pol = input2boardstandpol(inp)
			samples = self.roaches[board].get_samples(slot, stand, pol,
			                                          STAT_SAMP_SIZE)
			# Convert from int8 --> float32 before reducing
			samples = samples.astype(np.float32)
			op = args[2]
			return reduce_ops[op](samples)
		# TODO: BEAM_*
		#  BEAM%i_DELAY
		#  BEAM%i_GAIN
		#  BEAM%i_TUNING # Note: (ADP only)
		if args[0] == 'BOARD':
			board = args[1]-1
			if not (0 <= board < NBOARD):
				raise ValueError("Unknown board number %i"%(board+1))
			if args[2] == 'STAT': return None # TODO
			if args[2] == 'INFO': return None # TODO
			if args[2] == 'TEMP':
				temps = self.roaches[board].get_temperatures(slot).values()
				op = args[3]
				return reduce_ops[op](temps)
			if args[2] == 'FIRMWARE': return self.config['roach']['firmware']
			if args[2] == 'HOSTNAME': return self.roaches[board].host
			raise KeyError
		if args[0] == 'SERVER':
			svr = args[1]-1
			if not (0 <= svr < NSERVER):
				raise ValueError("Unknown server number %i"%(svr+1))
			if args[2] == 'HOSTNAME': return self.servers[svr].host
			# TODO: This request() should raise exceptions on failure
			# TODO: Change to .status(), .info()?
			if args[2] == 'STAT': return self.servers[svr].get_status()
			if args[2] == 'INFO': return self.servers[svr].get_info()
			if args[2] == 'TEMP':
				temps = self.servers[svr].get_temperatures(slot).values()
				op = args[3]
				return reduce_ops[op](temps)
			raise KeyError
		if args[0] == 'GLOBAL':
			if args[1] == 'TEMP':
				temps = []
				# Note: Actually just flattening lists, not summing
				temps += sum(self.roaches.get_temperatures(slot).values(), [])
				temps += sum(self.servers.get_temperatures(slot).values(), [])
				# Remove error values before reducing
				temps = [val for val in temps if not math.isnan(val)]
				if len(temps) == 0: # If all values were nan (exceptional!)
					temps = [float('nan')]
				op = args[2]
				return reduce_ops[op](temps)
			raise KeyError
		if key == 'CMD_STAT': return (slot,self.cmd_status[slot-1])
		raise KeyError
		
	def _pack_report_result(self, key, value):
		return {
			'SUMMARY':          lambda x: x[:7],
			'INFO':             lambda x: truncate_message(x, 256),
			'LASTLOG':          lambda x: truncate_message(x, 256),
			'SUBSYSTEM':        lambda x: x[:3],
			'SERIALNO':         lambda x: x[:5],
			'VERSION':          lambda x: truncate_message(x, 256),
			#'TBF_STATUS':
			#'TBF_TUNING_MASK':
			'NUM_TBN_BITS':     lambda x: struct.pack('>B', x),
			'NUM_DRX_TUNINGS':  lambda x: struct.pack('>B', x),
			'NUM_FREQ_CHANS':   lambda x: struct.pack('>H', x),
			#'NUM_BEAMS':
			'NUM_STANDS':       lambda x: struct.pack('>H', x),
			'NUM_BOARDS':       lambda x: struct.pack('>B', x),
			'NUM_SERVERS':      lambda x: struct.pack('>B', x),
			'BEAM_FIR_COEFFS':  lambda x: struct.pack('>B', x),
			#'T_NOMn:
			'FIR_CHAN_INDEX':   lambda x: struct.pack('>H', x),
			'FIR':              lambda x: x.astype('>h').tobytes(),
			'CLK_VAL':          lambda x: struct.pack('>I', x),
			'UTC_START':        lambda x: truncate_message(x, 256), # Not in spec
			'UPTIME':           lambda x: struct.pack('>I', x),     # Not in spec
			'STAT_SAMPLE_SIZE': lambda x: struct.pack('>I', x),
			'ANT_RMS':          lambda x: struct.pack('>f', x),
			'ANT_SAT':          lambda x: struct.pack('>i', x),
			'ANT_DCOFFSET':     lambda x: struct.pack('>f', x),
			'ANT_PEAK':         lambda x: struct.pack('>i', x),
			# TODO: Implement these BEAM requests
			#         Are these actually in the spec?
			#'BEAM_RMS':         lambda x: struct.pack('>f', x),
			#'BEAM_SAT':         lambda x: struct.pack('>i', x),
			#'BEAM_DCOFFSET':    lambda x: struct.pack('>f', x),
			#'BEAM_PEAK':        lambda x: struct.pack('>i', x),
			# TODO: In the spec this is >I ?
			'BOARD_STAT':       lambda x: struct.pack('>L', x),
			'BOARD_TEMP_MAX':   lambda x: struct.pack('>f', x),
			'BOARD_TEMP_MIN':   lambda x: struct.pack('>f', x),
			'BOARD_TEMP_AVG':   lambda x: struct.pack('>f', x),
			'BOARD_FIRMWARE':   lambda x: truncate_message(x, 256),
			'BOARD_HOSTNAME':   lambda x: truncate_message(x, 256),
			# TODO: SERVER_STAT
			'SERVER_TEMP_MAX':  lambda x: struct.pack('>f', x),
			'SERVER_TEMP_MIN':  lambda x: struct.pack('>f', x),
			'SERVER_TEMP_AVG':  lambda x: struct.pack('>f', x),
			'SERVER_SOFTWARE':  lambda x: truncate_message(x, 256),
			'SERVER_HOSTNAME':  lambda x: truncate_message(x, 256),
			'GLOBAL_TEMP_MAX':  lambda x: struct.pack('>f', x),
			'GLOBAL_TEMP_MIN':  lambda x: struct.pack('>f', x),
			'GLOBAL_TEMP_AVG':  lambda x: struct.pack('>f', x),
			'CMD_STAT':         lambda x: pack_reply_CMD_STAT(*x),
			'TBN_CONFIG_FREQ':  lambda x: struct.pack('>f', x),
			'TBN_CONFIG_FILTER':lambda x: struct.pack('>H', x),
			'TBN_CONFIG_GAIN':  lambda x: struct.pack('>H', x),
			'DRX_CONFIG_FREQ':  lambda x: struct.pack('>f', x),
			'DRX_CONFIG_FILTER':lambda x: struct.pack('>H', x),
			'DRX_CONFIG_GAIN':  lambda x: struct.pack('>H', x)
		}[key](value)
		
	def _format_report_result(self, key, value):
		format_function = defaultdict(lambda : str)
		format_function.update({
			'FIR':      pretty_print_bytes,
			'CMD_STAT': lambda x: '%i commands in previous slot' % len(x)
		})
		return format_function[key](value)
	
	def currently_processing(self, *cmds):
		return any([cmd in self.state['activeProcess'] for cmd in cmds])
		
	def process_command(self, msg):
		exec_delay = 2
		exec_slot  = msg.slot + exec_delay
		accept = True
		reply_data = ""
		exit_status = 0
		if msg.cmd == 'INI':
			if self.currently_processing('INI', 'SHT'):
				# TODO: This stuff could be tidied up a bit
				self.state['lastlog'] = ('INI: %s - %s is active and blocking'%
				                         ('Blocking operation in progress',
				                          self.state['activeProcess']))
				exit_status = 0x0C
			else:
				self.thread_pool.add_task(self.ini, msg.data)
		elif msg.cmd == 'CAL':
			if self.currently_processing('INI', 'SHT'):
				# TODO: This stuff could be tidied up a bit
				self.state['lastlog'] = ('CAL: %s - %s is active and blocking'%
				                         ('Blocking operation in progress',
				                          self.state['activeProcess']))
				exit_status = 0x0C
			else:
				self.thread_pool.add_task(self.adc_cal, msg.data)
		elif msg.cmd == 'SHT':
			if self.currently_processing('INI', 'SHT'):
				self.state['lastlog'] = ('SHT: %s - %s is active and blocking'%
				                         ('Blocking operation in progress',
				                          self.state['activeProcess']))
				exit_status = 0x0C
			else:
				self.thread_pool.add_task(self.sht, msg.data)
		elif msg.cmd == 'STP':
			mode = msg.data # TBN/TBF/BEAMn/COR
			if mode == 'DRX':
				# TODO: This is not actually part of the spec (useful for debugging?)
				exit_status = self.drx.stop()
			elif mode == 'TBN':
				exit_status = self.tbn.stop()
			elif mode == 'TBF':
				self.state['lastlog'] = "UNIMPLEMENTED STP request"
				exit_status = -1 # TODO: Implement this
			elif mode.startswith('BEAM'):
				self.state['lastlog'] = "UNIMPLEMENTED STP request"
				exit_status = -1 # TODO: Implement this
			elif mode == 'COR':
				self.state['lastlog'] = "UNIMPLEMENTED STP request"
				exit_status = -1 # TODO: Implement this
			else:
				self.state['lastlog'] = "Invalid STP request"
				exit_status = -1
		elif msg.cmd == 'DRX':
			exit_status = self.drx.process_command(msg)
		elif msg.cmd == 'TBF':
			exit_status = self.tbf.process_command(msg)
		elif msg.cmd == 'BAM':
			exit_status = self.bam.process_command(msg)
		elif msg.cmd == 'COR':
			exit_status = self.cor.process_command(msg)
		elif msg.cmd == 'TBN':
			exit_status = self.tbn.process_command(msg)
		else:
			exit_status = 0
			accept = False
			reply_data = 'Unknown command: %s' % msg.cmd
		if exit_status != 0:
			accept = False
			reply_data = "0x%02X! %s" % (exit_status, self.state['lastlog'])
		self.cmd_status[msg.slot].append( (msg.cmd, msg.ref, exit_status) )
		return accept, reply_data
