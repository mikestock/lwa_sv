#!/usr/bin/env python

import MCS2
import time
from Queue import Queue
import struct

MSG_REPLY_TIMEOUT = 5

def send_rpt(data):
	msg = MCS2.Msg(dst='ADP', cmd='RPT', data=data)
	print msg
	sender.put(msg)
	reply = receiver.get(timeout=MSG_REPLY_TIMEOUT)
	print reply
	#print reply.data, len(reply.data)
	if reply is not None and len(reply.data)-8 == 4:
		print struct.unpack('>f', reply.data[8:])

def send_msg(cmd, data=''):
        msg = MCS2.Msg(dst='ADP', cmd=cmd, data=data)
        print msg
        sender.put(msg)
        reply = receiver.get(timeout=MSG_REPLY_TIMEOUT)
	print reply

if __name__ == "__main__":
	import sys
	sender   = MCS2.MsgSender(("localhost",1742), subsystem='MCS')
	sender.input_queue = Queue()
	receiver = MCS2.MsgReceiver(("0.0.0.0",1743))
	sender.daemon = True
	receiver.daemon = True
	sender.start()
	receiver.start()
	
	if len(sys.argv) <= 1 or sys.argv[1] == 'status':
		pass
	elif sys.argv[1] == 'STP' or sys.argv[1] == 'stop':
		print "Sending STP TBN command"
		send_msg('STP', 'TBN')
		print "Sleeping"
		time.sleep(1)
	else:
		freq = float(sys.argv[1]) * 1e6
		filt = 7#1#float(sys.argv[2])
		gain = 2#1#float(sys.argv[3])
		if len(sys.argv) > 2:
			gain = float(sys.argv[2])
		print "Sending TBN command with freq = %f, gain = %f" % (freq,gain)
		send_msg('TBN', struct.pack('>fhh', freq, filt, gain))
		sys.exit(0) # HACK TESTING
		print "Sleeping"
		time.sleep(1)
	#send_rpt('SUMMARY')
	#send_rpt('INFO')
	#send_rpt('LASTLOG')
	#send_rpt('NUM_TBN_BITS')
	#send_rpt('TBN_CONFIG_FREQ')
	#send_rpt('TBN_CONFIG_FILTER')
	#send_rpt('TBN_CONFIG_GAIN')
	#print "Sleeping"
	#time.sleep(1)
	#send_rpt('SUMMARY')
	#send_rpt('INFO')
	#send_rpt('LASTLOG')
	#send_rpt('NUM_TBN_BITS')
	#send_rpt('TBN_CONFIG_FREQ')
	#send_rpt('TBN_CONFIG_FILTER')
	#send_rpt('TBN_CONFIG_GAIN')
	
	sender.request_stop()
	receiver.request_stop()
	sender.join()
	receiver.join()
	print "Done"
