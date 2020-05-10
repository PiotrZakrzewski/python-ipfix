#!/usr/bin/env python

import socket
from datetime import datetime
import ipfix.message
import ipfix.ie
import ipfix.template
from ipaddress import ip_address

msg = ipfix.message.MessageBuffer()
msg.begin_export(8304)
ipfix.ie.use_iana_default()
tmpl = ipfix.template.from_ielist(256,
        ipfix.ie.spec_list(("flowStartMilliseconds",
                            "sourceIPv4Address",
                            "destinationIPv4Address",
                            "packetDeltaCount")))
msg.add_template(tmpl)
msg.export_ensure_set(256)
rec = { "flowStartMilliseconds" : datetime.strptime("2020-05-10 14:00:00",
                                       "%Y-%m-%d %H:%M:%S"),
         "sourceIPv4Address" : ip_address("10.1.2.3"),
         "destinationIPv4Address" : ip_address("10.5.6.7"),
         "packetDeltaCount" : 27 }
msg.export_namedict(rec)

b = msg.to_bytes()
HOST = '127.0.0.1'
PORT = 4739

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.connect((HOST, PORT))
    s.sendall(b)
