#!/usr/bin/env python
# -*- coding: utf-8 -*-

from adp import MCS2 as MCS
from adp import Adp
from adp.AdpCommon import *
from adp import ISC

from bifrost.address import Address
from bifrost.udp_socket import UDPSocket
from bifrost.udp_capture import UDPCapture
from bifrost.ring import Ring
import bifrost.affinity as cpu_affinity
import bifrost.ndarray as BFArray
from bifrost.fft import Fft
from bifrost.unpack import unpack as Unpack
from bifrost.quantize import quantize as Quantize
from bifrost.libbifrost import bf

#import numpy as np
import signal
import logging
import time
import os
import argparse
import ctypes
import threading
import json
import socket
import struct
#import time
import datetime

#from numpy.fft import ifft
#from scipy import ifft
from scipy.fftpack import ifft

FILTER2BW = {1:    1000, 
	     2:    3125, 
	     3:    6250, 
	     4:   12500, 
	     5:   25000, 
	     6:   50000, 
	     7:  100000,
	     8:  200000, 
	     9:  400000,
	    10:  800000,
	    11: 1600000}
FILTER2CHAN = {1:    1000/25000, 
	       2:    3125/25000, 
	       3:    6250/25000, 
	       4:   12500/25000, 
	       5:   25000/25000, 
	       6:   50000/25000, 
	       7:  100000/25000,
	       8:  200000/25000, 
	       9:  400000/25000,
	      10:  800000/25000,
	      11: 1600000/25000}

__version__    = "0.1"
__date__       = '$LastChangedDate: 2015-07-23 15:44:00 -0600 (Fri, 25 Jul 2014) $'
__author__     = "Ben Barsdell, Daniel Price, Jayce Dowell"
__copyright__  = "Copyright 2015, The LWA-SV Project"
__credits__    = ["Ben Barsdell", "Daniel Price", "Jayce Dowell"]
__license__    = "Apache v2"
__maintainer__ = "Jayce Dowell"
__email__      = "jdowell at unm"
__status__     = "Development"

#{"nbit": 4, "nchan": 136, "nsrc": 16, "chan0": 1456, "time_tag": 288274740432000000}
class CaptureOp(object):
	def __init__(self, log, *args, **kwargs):
		self.log    = log
		self.args   = args
		self.kwargs = kwargs
		self.utc_start = self.kwargs['utc_start']
		del self.kwargs['utc_start']
		self.shutdown_event = threading.Event()
		## HACK TESTING
		#self.seq_callback = None
	def shutdown(self):
		self.shutdown_event.set()
	def seq_callback(self, seq0, chan0, nchan, nsrc,
	                 time_tag_ptr, hdr_ptr, hdr_size_ptr):
		timestamp0 = int((self.utc_start - ADP_EPOCH).total_seconds())
		time_tag0  = timestamp0 * int(FS)
		time_tag   = time_tag0 + seq0*(int(FS)//int(CHAN_BW))
		print "++++++++++++++++ seq0     =", seq0
		print "                 time_tag =", time_tag
		time_tag_ptr[0] = time_tag
		hdr = {
			'time_tag': time_tag,
			'seq0':     seq0, 
			'chan0':    chan0,
			'nchan':    nchan,
			'cfreq':    (chan0 + 0.5*(nchan-1))*CHAN_BW,
			'bw':       nchan*CHAN_BW,
			'nsrc':     nsrc, 
			'nstand':   nsrc*16,
			#'stand0':   src0*16, # TODO: Pass src0 to the callback too(?)
			'npol':     2,
			'complex':  True,
			'nbit':     4
		}
		print "******** CFREQ:", hdr['cfreq']
		hdr_str = json.dumps(hdr)
		# TODO: Can't pad with NULL because returned as C-string
		#hdr_str = json.dumps(hdr).ljust(4096, '\0')
		#hdr_str = json.dumps(hdr).ljust(4096, ' ')
		self.header_buf = ctypes.create_string_buffer(hdr_str)
		hdr_ptr[0]      = ctypes.cast(self.header_buf, ctypes.c_void_p)
		hdr_size_ptr[0] = len(hdr_str)
		return 0
	def main(self):
		seq_callback = bf.BFudpcapture_sequence_callback(self.seq_callback)
		with UDPCapture(*self.args,
		                sequence_callback=seq_callback,
		                **self.kwargs) as capture:
			while not self.shutdown_event.is_set():
				status = capture.recv()
				#print status
		del capture

class UnpackOp(object):
	def __init__(self, log, iring, oring, ntime_gulp=2500, core=-1):
		self.log = log
		self.iring = iring
		self.oring = oring
		self.ntime_gulp = ntime_gulp
		self.core = core
	def main(self):
		cpu_affinity.set_core(self.core)
		with self.oring.begin_writing() as oring:
			for iseq in self.iring.read():
				#print "HEADER:", iseq.header.tostring()
				ihdr = json.loads(iseq.header.tostring())
				nchan  = ihdr['nchan']
				nstand = ihdr['nstand']
				npol   = ihdr['npol']
				igulp_size = self.ntime_gulp*nchan*nstand*npol
				ishape = (self.ntime_gulp,nchan,nstand,npol,1)
				ogulp_size = igulp_size * 2
				oshape = (self.ntime_gulp,nchan,nstand,npol,2)
				self.iring.resize(igulp_size)
				self.oring.resize(ogulp_size)
				ohdr = ihdr.copy()
				ohdr['nbit'] = 8
				ohdr_str = json.dumps(ohdr)
				with oring.begin_sequence(time_tag=iseq.time_tag, header=ohdr_str) as oseq:
					for ispan in iseq.read(igulp_size):
						if ispan.size < igulp_size:
							continue # Ignore final gulp
						with oseq.reserve(ogulp_size) as ospan:
							## Setup and load
							idata = ispan.data_view(np.int8).reshape(ishape)
							odata = ospan.data_view(np.int8).reshape(oshape)
							
							## Fix the type
							bfidata = BFArray(shape=idata.shape, dtype='ci4', native=False, buffer=idata.ctypes.data, space='cuda_host')
							bfodata = BFArray(shape=idata.shape, dtype='ci8', space='cuda_host')
							
							## Unpack
							Unpack(bfidata, bfodata)
							
							## Save
							odata[...] = bfodata.view(np.int8)

class TEngineOp(object):
	def __init__(self, log, iring, oring, ntime_gulp=2500,# ntime_buf=None,
	             guarantee=True, core=-1):
		self.log = log
		self.iring = iring
		self.oring = oring
		self.ntime_gulp = ntime_gulp
		#if ntime_buf is None:
		#	ntime_buf = self.ntime_gulp*3
		#self.ntime_buf = ntime_buf
		self.guarantee = guarantee
		self.core = core
		
		self.configMessage = ISC.TBNConfigurationClient(addr=('adp',5832))
		self.gain = 2
		self.filt = 7
		self.nchan_out = FILTER2CHAN[7]
		self.phaseRot = 1
		
	@ISC.logException
	def updateConfig(self, config, hdr):
		if config:
			self.log.info("TEngine: New configuration received: %s", str(config))
			freq, filt, gain = config
			self.rFreq = freq
			self.filt = filt
			self.nchan_out = FILTER2CHAN[filt]
			self.gain = gain
			
			fDiff = freq - (hdr['chan0'] + 0.5*(hdr['nchan']-1))*CHAN_BW - CHAN_BW / 2
			self.log.info("TEngine: Tuning offset is %.3f kHz to be corrected with phase rotation", fDiff)
			
			self.phaseRot = np.exp(-2j*np.pi*fDiff/(self.nchan_out*CHAN_BW)*np.arange(self.ntime_gulp*self.nchan_out))
			self.phaseRot.shape += (1,1)
			
			return True
		else:
			return False
			
	@ISC.logException
	def main(self):
		cpu_affinity.set_core(self.core)
		
		with self.oring.begin_writing() as oring:
			for iseq in self.iring.read(guarantee=self.guarantee):
				ihdr = json.loads(iseq.header.tostring())
				
				self.updateConfig( self.configMessage(), ihdr )
				
				nsrc   = ihdr['nsrc']
				nchan  = ihdr['nchan']
				nstand = ihdr['nstand']
				npol   = ihdr['npol']
				
				igulp_size = self.ntime_gulp*nchan*nstand*npol*2		# 8+8 complex
				ishape = (self.ntime_gulp,nchan,nstand,npol,2)
				ogulp_size = self.ntime_gulp*self.nchan_out*nstand*npol*2	# 8+8 complex
				oshape = (self.ntime_gulp*self.nchan_out,nstand,npol,2)
				self.iring.resize(igulp_size)
				self.oring.resize(ogulp_size)#, obuf_size)
				ohdr = {}
				ohdr['time_tag'] = ihdr['time_tag']
				try:
					ohdr['cfreq']    = self.rFreq
				except AttributeError:
					ohdr['cfreq']    = (ihdr['chan0'] + 0.5*(ihdr['nchan']-1))*CHAN_BW - CHAN_BW / 2
				ohdr['bw']       = self.nchan_out*CHAN_BW
				ohdr['gain']     = self.gain
				ohdr['filter']   = self.filt
				ohdr['nstand']   = nstand
				ohdr['npol']     = npol
				ohdr['complex']  = True
				ohdr['nbit']     = 8
				ohdr_str = json.dumps(ohdr)
				
				with oring.begin_sequence(time_tag=iseq.time_tag, header=ohdr_str) as oseq:
					for ispan in iseq.read(igulp_size):
						if ispan.size < igulp_size:
							continue # Ignore final gulp
							
						self.updateConfig( self.configMessage(), ihdr )
						
						with oseq.reserve(ogulp_size) as ospan:
							## Setup and load
							idata = ispan.data_view(np.int8).reshape(ishape)
							odata = ospan.data_view(np.int8).reshape((1,)+oshape)
							tdata  = idata[...,0].astype(np.float32) + 1j*idata[...,1].astype(np.float32)
							
							## Prune and shift the data ahead of the IFFT
							tdata = tdata[:,nchan/2-self.nchan_out/2:nchan/2+self.nchan_out/2]
							tdata = np.fft.fftshift(tdata, axes=1)
							
							## IFFT
							tdata = ifft(tdata, axis=1).astype(np.complex64)
							tdata = BFArray(tdata, space='system')
							#gdata = tdata.copy(space='cuda')
							#try:
							#	bfft.execute(gdata, gdata, inverse=True)
							#except NameError:
							#	bfft = Fft()
							#	bfft.init(gdata, gdata, axes=1)
							#	bfft.execute(gdata, gdata, inverse=True)
							#tdata = gdata.copy(space='system')
							
							## Phase rotation
							tdata = tdata.reshape((-1,nstand,npol))
							tdata *= self.phaseRot
							
							## Scaling
							tdata *= 128./(2**self.gain * np.sqrt(self.nchan_out))
							
							## Quantization
							try:
								Quantize(tdata, qdata)
							except NameError:
								qdata = BFArray(shape=tdata.shape, dtype='ci8')
								Quantize(tdata, qdata)
								
							## Save
							odata[...] = qdata.view(np.int8).reshape((1,)+oshape)
							
							#tdata = quantize_complex8b(tdata) # Note: dtype is now real
							#odata = ospan.data_view(np.int8).reshape((1,)+oshape)
							#odata[...] = tdata
							
				# Clean-up
				try:
					del bfft
					del qdata
				except NameError:
					pass

def gen_tbn_header(stand, pol, cfreq, gain, time_tag, time_tag0, bw=100e3):
	nframe_per_sample = int(FS) // int(bw)
	nframe_per_packet = nframe_per_sample * TBN_NSAMPLE_PER_PKT
	sync_word    = 0xDEC0DE5C
	idval        = 0x0
	frame_num_wrap = 10 * int(bw) # 10 secs = 4e6, fits within a uint24
	frame_num    = ((time_tag - time_tag0) // nframe_per_packet) % frame_num_wrap + 1 # Packet sequence no.
	id_frame_num = idval << 24 | frame_num
	assert( 0 <= cfreq < FS )
	tuning_word  = int(round(cfreq / FS * 2**32))
	tbn_id       = (pol + NPOL*stand) + 1
	gain         = gain
	#if stand == 0 and pol == 0:
	#	print cfreq, bw, gain, time_tag, time_tag0
	#	print nframe_per_sample, nframe_per_packet
	return struct.pack('>IIIhhq',
	                   sync_word,
	                   id_frame_num,
	                   tuning_word,
	                   tbn_id,
	                   gain,
	                   time_tag)

class PacketizeOp(object):
	# Note: Input data are: [time,beam,pol,iq]
	def __init__(self, log, iring, nroach, roach0, addr, port, npkt_gulp=128, core=-1):
		self.log   = log
		self.iring = iring
		self.nroach = nroach
		self.roach0 = roach0
		self.sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self.sock.connect((addr,port))
		self.npkt_gulp = npkt_gulp
		self.core = core
		
		self.sync_pipelines = MCS.Synchronizer('TBN')
		
	def main(self):
		cpu_affinity.set_core(self.core)
		
		stand0 = self.roach0 * 16 # TODO: Get this less hackily
		ntime_pkt     = TBN_NSAMPLE_PER_PKT
		ntime_gulp    = self.npkt_gulp * ntime_pkt
		ninput_max    = self.nroach*32
		gulp_size_max = ntime_gulp * ninput_max * 2
		self.iring.resize(gulp_size_max)
		
		for isequence in self.iring.read():
			hdr = json.loads(isequence.header.tostring())
			#print 'PacketizeOp', hdr
			cfreq  = hdr['cfreq']
			bw     = hdr['bw']
			gain   = hdr['gain']
			nstand = hdr['nstand']
			#stand0 = hdr['stand0']
			npol   = hdr['npol']
			time_tag0 = isequence.time_tag
			time_tag  = time_tag0
			gulp_size = ntime_gulp*nstand*npol*2
			
			for ispan in isequence.read(gulp_size):
				if ispan.size < gulp_size:
					continue # Ignore final gulp
					
				shape = (-1,nstand,npol,2)
				data = ispan.data_view(np.int8).reshape(shape)
				#self.sync_pipelines(time_tag)
				for t in xrange(0, ntime_gulp, ntime_pkt):
					self.sync_pipelines(time_tag)
					for stand in xrange(nstand):
						for pol in xrange(npol):
							pktdata = data[t:t+ntime_pkt,stand,pol,:]
							#pktdata = pktdata[...,::-1] # WAR: Swap I/Q
							#assert( len(pktdata) == ntime_pkt )
							time_tag_cur = time_tag + int(round(float(t)/bw*FS))
							hdr = gen_tbn_header(stand0+stand, pol, cfreq, gain,
							                     time_tag_cur, time_tag0, bw)
							pkt = hdr + pktdata.tostring()
							try:
								self.sock.send(pkt)
							except socket.error:
								pass
								
				time_tag += int(round(float(ntime_gulp)/bw*FS))

def get_utc_start():
	got_utc_start = False
	while not got_utc_start:
		try:
			with MCS.Communicator() as adp_control:
				utc_start = adp_control.report('UTC_START')
				# Check for valid timestamp
				utc_start_dt = datetime.datetime.strptime(utc_start, DATE_FORMAT)
			got_utc_start = True
		except Exception as ex:
			print ex
			time.sleep(1)
	#print "UTC_START:", utc_start
	#return utc_start
	return utc_start_dt

def get_numeric_suffix(s):
	i = 0
	while True:
		if len(s[i:]) == 0:
			raise ValueError("No numeric suffix in string '%s'" % s)
		try: return int(s[i:])
		except ValueError: i += 1

def partition_balanced(nitem, npart, part_idx):
	rem = nitem % npart
	part_nitem  = nitem / npart + (part_idx < rem)
	part_offset = (part_idx*part_nitem if part_idx < rem else
	               rem*(part_nitem+1) + (part_idx-rem)*part_nitem)
	return part_nitem, part_offset

def partition_packed(nitem, npart, part_idx):
	part_nitem  = (nitem-1) / npart + 1
	part_offset = part_idx * part_nitem
	part_nitem  = min(part_nitem, nitem-part_offset)
	return part_nitem, part_offset

def main(argv):
	parser = argparse.ArgumentParser(description='LWA-SV ADP TBN Service')
	parser.add_argument('-c', '--configfile', default='adp_config.json', help='Specify config file')
	parser.add_argument('-l', '--logfile',    default=None,              help='Specify log file')
	parser.add_argument('-d', '--dryrun',     action='store_true',       help='Test without acting')
	parser.add_argument('-v', '--verbose',    action='count', default=0, help='Increase verbosity')
	parser.add_argument('-q', '--quiet',      action='count', default=0, help='Decrease verbosity')
	args = parser.parse_args()
	
	config = Adp.parse_config_file(args.configfile)
	
	log = logging.getLogger(__name__)
	logFormat = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s',
	                              datefmt='%Y-%m-%d %H:%M:%S')
	logFormat.converter = time.gmtime
	if args.logfile is None:
		logHandler = logging.StreamHandler(sys.stdout)
	else:
		logHandler = Adp.AdpFileHandler(config, args.logfile)
	logHandler.setFormatter(logFormat)
	log.addHandler(logHandler)
	verbosity = args.verbose - args.quiet
	if   verbosity >  0: log.setLevel(logging.DEBUG)
	elif verbosity == 0: log.setLevel(logging.INFO)
	elif verbosity <  0: log.setLevel(logging.WARNING)
	
	short_date = ' '.join(__date__.split()[1:4])
	log.info("Starting %s with PID %i", argv[0], os.getpid())
	log.info("Cmdline args: \"%s\"", ' '.join(argv[1:]))
	log.info("Version:      %s", __version__)
	log.info("Last changed: %s", short_date)
	log.info("Current MJD:  %f", Adp.MCS2.slot2mjd())
	log.info("Current MPM:  %i", Adp.MCS2.slot2mpm())
	log.info("Config file:  %s", args.configfile)
	log.info("Log file:     %s", args.logfile)
	log.info("Dry run:      %r", args.dryrun)
	
	shutdown_event = threading.Event()
	def handle_signal_terminate(signum, frame):
		SIGNAL_NAMES = dict((k, v) for v, k in \
		                    reversed(sorted(signal.__dict__.items()))
		                    if v.startswith('SIG') and \
		                    not v.startswith('SIG_'))
		log.warning("Received signal %i %s", signum, SIGNAL_NAMES[signum])
		ops[0].shutdown()
		shutdown_event.set()
	for sig in [signal.SIGHUP,
	            signal.SIGINT,
	            signal.SIGQUIT,
	            signal.SIGTERM,
	            signal.SIGTSTP]:
		signal.signal(sig, handle_signal_terminate)
	
	log.info("Waiting to get UTC_START")
	utc_start_dt = get_utc_start()
	log.info("UTC_START:    %s", utc_start_dt.strftime(DATE_FORMAT))
	
	hostname = socket.gethostname()
	server_idx = get_numeric_suffix(hostname) - 1
	log.info("Hostname:     %s", hostname)
	log.info("Server index: %i", server_idx)
	
	pipeline_idx = config['tbn']['pipeline_idx']
	recorder_idx = config['tbn']['recorder_idx']
	iaddr  = config['server']['data_ifaces'][pipeline_idx]
	iport  = config['server']['data_ports' ][pipeline_idx]
	oaddr  = config['host']['recorders'][recorder_idx]
	oport  = config['recorder']['port']
	nroach_tot = len(config['host']['roaches'])
	nserver    = len(config['host']['servers'])
	tbn_servers = config['host']['servers-tbn']
	server_data_host = config['host']['servers-data'][server_idx]
	nroach = len([srv for srv in tbn_servers if srv == server_data_host])
	roach0 = [i for (i,srv) in enumerate(tbn_servers) if srv == server_data_host][0]
	core0 = config['tbn']['first_cpu_core']
	
	log.info("Src address:  %s:%i", iaddr, iport)
	log.info("Dst address:  %s:%i", oaddr, oport)
	log.info("Roaches:      %i-%i", roach0+1, roach0+nroach)
	
	# Note: Capture uses Bifrost address+socket objects, while output uses
	#         plain Python address+socket objects.
	iaddr = Address(iaddr, iport)
	isock = UDPSocket()
	isock.bind(iaddr)
	
	capture_ring = Ring()
	unpack_ring = Ring()
	tengine_ring = Ring()
	
	osock = None # TODO
	
	ops = []
	core = core0
	ops.append(CaptureOp(log, fmt="chips", sock=isock, ring=capture_ring,
	                     nsrc=nroach, src0=roach0, max_payload_size=9000,
	                     buffer_ntime=25000, slot_ntime=25000, core=core,
	                     utc_start=utc_start_dt))
	core += 1
	ops.append(UnpackOp(log, capture_ring, unpack_ring, 
	                    core=core))
	core += 1
	ops.append(TEngineOp(log, unpack_ring, tengine_ring,
	                     core=core))
	core += 1
	ops.append(PacketizeOp(log, tengine_ring,
	                       nroach=nroach, roach0=roach0,
	                       addr=oaddr, port=oport,
	                       npkt_gulp=10, core=core))
	core += 1
	
	threads = [threading.Thread(target=op.main) for op in ops]
	
	log.info("Launching %i thread(s)", len(threads))
	for thread in threads:
		thread.daemon = True
		thread.start()
	log.info("Waiting for threads to finish")
	while not shutdown_event.is_set():
		signal.pause()
	for thread in threads:
		thread.join()
	log.info("All done")
	return 0

if __name__ == '__main__':
	import sys
	sys.exit(main(sys.argv))
