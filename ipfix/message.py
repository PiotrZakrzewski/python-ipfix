"""
Provides the MessageBuffer class for encoding and decoding IPFIX messages.

This interface allows direct control over Messages; for reading or writing
records automatically from/to streams, see :mod:`reader` and :mod:`writer`,
respectively.

To create a message buffer:

>>> import ipfix.message
>>> msg = ipfix.message.MessageBuffer()
>>> msg
<MessageBuffer domain 0 length 0>

To prepare the buffer to write records:

>>> msg.begin_export(8304)
>>> msg
<MessageBuffer domain 8304 length 16 (writing)>

Note that the buffer grows to contain the message header.

To write records to the buffer, first you'll need a template:

>>> import ipfix.ie
>>> ipfix.ie.use_iana_default()
>>> import ipfix.template
>>> tmpl = ipfix.template.from_ielist(256, 
...        ipfix.ie.spec_list(("flowStartMilliseconds",
...                            "sourceIPv4Address",
...                            "destinationIPv4Address",
...                            "packetDeltaCount")))
>>> tmpl
<Template ID 256 count 4 scope 0>

To add the template to the message:

>>> msg.add_template(tmpl)
>>> msg
<MessageBuffer domain 8304 length 40 (writing set 2)>

Note that :meth:`MessageBuffer.add_template` exports the template when it 
is written by default, and that the current set ID is 2 (template set).

Now, a set must be created to add records to the message; the set ID must match
the ID of the template. MessageBuffer automatically uses the template matching
the set ID for record encoding.

>>> msg.export_new_set(256)
>>> msg
<MessageBuffer domain 8304 length 44 (writing set 256)>

Records can be added to the set either as dictionaries keyed by IE name:

>>> from datetime import datetime
>>> from ipaddress import ip_address
>>> rec = { "flowStartMilliseconds" : datetime.strptime("2013-06-21 14:00:00", 
...                                       "%Y-%m-%d %H:%M:%S"),
...         "sourceIPv4Address" : ip_address("10.1.2.3"),
...         "destinationIPv4Address" : ip_address("10.5.6.7"),
...         "packetDeltaCount" : 27 }
>>> msg.export_namedict(rec)
>>> msg
<MessageBuffer domain 8304 length 68 (writing set 256)>

or as tuples in template order:

>>> rec = (datetime.strptime("2013-06-21 14:00:02", "%Y-%m-%d %H:%M:%S"),
...        ip_address("10.8.9.11"), ip_address("10.12.13.14"), 33)
>>> msg.export_tuple(rec)
>>> msg
<MessageBuffer domain 8304 length 92 (writing set 256)>

Attempts to write past the end of the message (set via the mtu parameter, 
default 65535) result in :exc:`EndOfMessage` being raised.

Messages can be written to a stream using :meth:`MessageBuffer.write_message`, 
or dumped to a byte array for transmission using :meth:`MessageBuffer.to_bytes`.
The message must be reset before starting to write again.

>>> b = msg.to_bytes()
>>> msg.begin_export()
>>> msg 
<MessageBuffer domain 8304 length 16 (writing)>

Reading happens more or less in reverse. To begin, a message is read from a
byte array using :meth:`MessageBuffer.from_bytes`, or from a stream using 
:meth:`MessageBuffer.read_message`.

>>> msg.from_bytes(b)
>>> msg
<MessageBuffer domain 8304 length 92 (deframed 2 sets)>

Both of these methods scan the message in advance to find the sets within
the message. The records within these sets can then be accessed by iterating
over the message. As with export, the records can be accessed as a dictionary 
mapping IE names to values or as tuples. The dictionary interface is
designed for general IPFIX processing applications, such as collectors 
accepting many types of data, or diagnostic tools for debugging IPFIX export. 

>>> iter = msg.namedict_iterator()
>>> sorted(next(iter).items())
[('destinationIPv4Address', IPv4Address('10.5.6.7')), ('flowStartMilliseconds', datetime.datetime(2013, 6, 21, 12, 0)), ('packetDeltaCount', 27), ('sourceIPv4Address', IPv4Address('10.1.2.3'))]
>>> sorted(next(iter).items())
[('destinationIPv4Address', IPv4Address('10.12.13.14')), ('flowStartMilliseconds', datetime.datetime(2013, 6, 21, 12, 0, 2)), ('packetDeltaCount', 33), ('sourceIPv4Address', IPv4Address('10.8.9.11'))]
>>> next(iter)
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
StopIteration

The tuple interface for reading messages is designed for applications with a
specific internal data model. It can be much faster than the dictionary
interface, as it skips decoding of IEs not requested by the caller, and can
skip entire sets not containing all the requested IEs. Requested IEs are
specified as an :class:`ipfix.ie.InformationElementList` instance, from 
:func:`ie.spec_list()`:

>>> msg = ipfix.message.MessageBuffer()
>>> msg.from_bytes(b)
>>> ielist = ipfix.ie.spec_list(("flowStartMilliseconds", "packetDeltaCount"))
>>> iter = msg.tuple_iterator(ielist)
>>> next(iter)
[datetime.datetime(2013, 6, 21, 12, 0), 27]
>>> next(iter)
[datetime.datetime(2013, 6, 21, 12, 0, 2), 33]
>>> next(iter)
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
StopIteration

.. warning:: A MessageBuffer using the tuple interface can *only* be used for 
             a single IE list; changing lists, or switching between the 
             dictionary and tuple interfaces on a given MessageBuffer, 
             will result in undefined behavior.

"""

from . import template
from .template import IpfixEncodeError, IpfixDecodeError

import operator
import functools
import struct
from datetime import datetime
from warnings import warn

_sethdr_st = struct.Struct("!HH")
_msghdr_st = struct.Struct("!HHLLL")

class EndOfMessage(Exception):
    """
    Exception raised when a write operation on a Message
    fails because there is not enough space in the message.
    
    """
    def __init__(self, *args):
        super().__init__(args)

def accept_all_templates(tmpl):
    return True    

class MessageBuffer:
    """
    Implements a buffer for reading or writing IPFIX messages.
    
    """
    def __init__(self):
        """Create a new MessageBuffer instance."""
        self.mbuf = memoryview(bytearray(65536))

        self.length = 0
        self.sequence = None
        self.export_epoch = None
        self.odid = 0
        self.streamid = 0

        self.templates = {}
        self.accepted_tids = set()
        self.sequences = {}
        
        self.setlist = []

        self.auto_export_time = True
        self.cursetoff = 0
        self.cursetid = None
        self.curtmpl = None
        
        self.mtu = 65535
        
    def __repr__(self):
        if self.cursetid:
            addinf = " (writing set "+str(self.cursetid)+")"
        elif self.setlist:
            addinf = " (deframed "+str(len(self.setlist))+" sets)"
        elif self.length:
            addinf = " (writing)"
        else:
            addinf = ""
        
        return "<MessageBuffer domain "+str(self.odid)+\
               " length "+str(self.length)+addinf+">"
        
    def get_export_time(self):
        """
        Return the export time of this message. When reading, returns the 
        export time as read from the message header. When writing, this is 
        the argument of the last call to :meth:`set_export_time`, or, if 
        :attr:auto_export_time is True, the time of the last message
        export.
        
        :returns: export time of the last message read/written.
        
        """
        return datetime.utcfromtimestamp(self.export_epoch)

    def set_export_time(self, dt=None):
        """
        Set the export time for the next message written with 
        :meth:`write_message` or :meth:`to_bytes`. Disables automatic export 
        time updates. By default, sets the export time to the current time.
        
        :param dt: export time to set, as a datetime
        
        """
        if not dt:
            dt = datetime.utcnow()
        self.export_epoch = int(dt.timestamp())        
        self.auto_export_time = False
                
    def _increment_sequence(self):
        self.sequences.setdefault((self.odid, self.streamid), 0)
        self.sequences[(self.odid, self.streamid)] += 1
        
    def _scan_setlist(self):
        # We've read a message. Discard all export state.
        self.cursetoff = 0
        self.cursetid = None
        self.curtmpl = None        
        
        # Clear the setlist and start from the beginning of the body
        self.setlist.clear()
        offset = _msghdr_st.size
        
        while (offset < self.length):
            (setid, setlen) = _sethdr_st.unpack_from(self.mbuf, offset)
            if offset + setlen > self.length:
                raise IPFIXDecodeError("Set too long for message")
            self.setlist.append((offset, setid, setlen))
            offset += setlen
        
    def read_message(self, stream):
        """Read a IPFIX message from a stream.
        
        This populates message header fields and the internal setlist.
        Call for each new message before iterating over records when reading
        from a stream.
        
        :param stream: stream to read from
        :raises: IpfixDecodeError
        
        """
        
        # deframe and parse message header 
        msghdr = stream.read(_msghdr_st.size)
        if (len(msghdr) == 0):
            raise EOFError()
        elif (len(msghdr) < _msghdr_st.size):
            raise IpfixDecodeError("Short read in message header ("+ 
                                       str(len(msghdr)) +")")

        self.mbuf[0:_msghdr_st.size] = msghdr
        (version, self.length, self.sequence, self.export_epoch, self.odid) = \
                _msghdr_st.unpack_from(self.mbuf, 0)
        
        # verify version and length
        if version != 10:
            raise IpfixDecodeError("Illegal or unsupported version " + 
                                       str(version))
        
        if self.length < 20:
            raise IpfixDecodeError("Illegal message length" + 
                                       str(self.length))
            
        # read the rest of the message into the buffer
        msgbody = stream.read(self.length-_msghdr_st.size)
        if len(msgbody) < self.length - _msghdr_st.size:
            raise IpfixDecodeError("Short read in message body (got "+
                                   str(len(msgbody))+", expected "+
                                   str(self.length - _msghdr_st.size)+")")
        self.mbuf[_msghdr_st.size:self.length] = msgbody
        
        # populate setlist
        self._scan_setlist()
            
    def from_bytes(self, bytes):
        """Read an IPFIX message from a byte array.
        
        This populates message header fields and the internal setlist.
        Call for each new message before iterating over records when reading
        from a byte array.        

        :param bytes: a byte array containing a complete IPFIX message.
        :raises: IpfixDecodeError
        
        """
        # make a copy of the byte array
        self.mbuf[0:len(bytes)] = bytes

        # parse message header 
        if (len(bytes) < _msghdr_st.size):
            raise IpfixDecodeError("Message too short ("+str(len(msghdr)) +")")

        (version, self.length, self.sequence, self.export_epoch, self.odid) = \
                _msghdr_st.unpack_from(self.mbuf, 0)
        
        # verify version and length
        if version != 10:
            raise IpfixDecodeError("Illegal or unsupported version " + 
                                   str(version))
        
        if self.length < 20:
            raise IpfixDecodeError("Illegal message length" + str(self.length))
        
        # populate setlist
        self._scan_setlist()
            
    def record_iterator(self, 
                        decode_fn=template.Template.decode_namedict_from, 
                        tmplaccept_fn=accept_all_templates, 
                        recinf = None):
        """
        Low-level interface to record iteration.
        
        Iterate over records in an IPFIX message previously read with 
        :meth:`read_message()` or :meth:`from_bytes()`. Automatically handles 
        templates in set order. By default, iterates over each record in the 
        stream as a dictionary mapping IE name to value 
        (i.e., the same as :meth:`namedict_iterator`)
        
        :param decode_fn: Function used to decode a record; 
                          must be an (unbound) instance method of the 
                          :class:`ipfix.template.Template` class.
        :param tmplaccept_fn: Function returning True if the given template
                              is of interest to the caller, False if not.
                              Default accepts all templates. Sets described by
                              templates for which this function returns False
                              will be skipped.
        :param recinf: Record information opaquely passed to decode function
        :returns: an iterator over records decoded by decode_fn.

        """        
        for (offset, setid, setlen) in self.setlist:
            setend = offset + setlen
            offset += _sethdr_st.size # skip set header in decode
            if setid == 2 or setid == 3:
                while offset < setend:
                    (tmpl, offset) = template.decode_template_from(
                                              self.mbuf, offset, setid)
                    # FIXME handle withdrawal
                    self.templates[(self.odid, tmpl.tid)] = tmpl
                    if tmplaccept_fn(tmpl):
                        self.accepted_tids.add((self.odid, tmpl.tid))
                    else:
                        self.accepted_tids.discard((self.odid, tmpl.tid))
                    
            elif setid < 256:
                warn("skipping illegal set id "+setid)
            elif (self.odid, setid) in self.accepted_tids:
                try:
                    tmpl = self.templates[(self.odid, setid)]
                    while offset + tmpl.minlength <= setend:
                        (rec, offset) = decode_fn(tmpl, self.mbuf, offset, 
                                                  recinf = recinf)
                        yield rec
                        self._increment_sequence()
                except KeyError:
                    #FIXME provide set buffer for sets without templates
                    pass
            else:
                #FIXME disable sequence checking on skipped sets
                pass

    def namedict_iterator(self):
        """
        Iterate over all records in the Message, as dicts mapping IE names
        to values.
        
        :returns: a name dictionary iterator
        
        """
        
        return self.record_iterator(
                decode_fn = template.Template.decode_namedict_from)
    
    def tuple_iterator(self, ielist):
        """
        Iterate over all records in the Message containing all the IEs in 
        the given ielist. Records are returned as tuples in ielist order.
        
        :param ielist: an instance of :class:`ipfix.ie.InformationElementList`
                       listing IEs to return as a tuple
        :returns: a tuple iterator for tuples as in the list
        
        """
        tmplaccept_fn = lambda tmpl: \
                functools.reduce(operator.__and__, 
                                 (ie in tmpl.ies for ie in ielist))
        return self.record_iterator(
                decode_fn = template.Template.decode_tuple_from, 
                tmplaccept_fn = tmplaccept_fn, 
                recinf = ielist)          

    def to_bytes(self):
        """
        Convert this MessageBuffer to a byte array, suitable for writing
        to a binary file, socket, or datagram. Finalizes the message by
        rewriting the message header with current length, and export time. 
        
        :returns: message as a byte array
        
        """

        # Close final set 
        self.export_close_set()
        
        # Update export time if necessary
        if self.auto_export_time:
            self.export_epoch = int(datetime.utcnow().timestamp())
        
        # Update message header in buffer
        _msghdr_st.pack_into(self.mbuf, 0, 10, self.length, 
                             self.sequence, self.export_epoch, self.odid)
    
        
        return self.mbuf[0:self.length].tobytes()

    def write_message(self, stream):
        """
        Convenience method to write a message to a stream; see :meth:`to_bytes`.
        """
        stream.write(self.to_bytes())

    def add_template(self, tmpl, export=True):
        """
        Add a template to this MessageBuffer. Adding a template makes it 
        available for use for exporting records; see :meth:`export_new_set`. 
        
        :param tmpl: the template to add
        :param export: If True, export this template to the MessageBuffer
                       after adding it.
        """
        self.templates[(self.odid, tmpl.tid)] = tmpl
        if export:
            self.export_template(tmpl)
    
    def delete_template(self, tid, export=True):
        setid = self.templates[self.odid, tid].native_setid()
        del(self.templates[self.odid, tid])
        if export:
            self.export_template_withdrawal(setid, tid)
    
    def begin_export(self, odid=None):
        # We're exporting. Clear setlist from any previously read message.
        self.setlist.clear()
        
        # Set sequence number
        self.sequences.setdefault((self.odid, self.streamid), 0) # FIXME why do we need this?
        self.sequence = self.sequences[(self.odid, self.streamid)]
        
        # set new domain if necessary
        if odid:
            self.odid = odid
        
        # reset message and zero header
        self.length = _msghdr_st.size
        self.cursetoff = self.length
        self.mbuf[0:_msghdr_st.size] = bytes([0] * _msghdr_st.size)
    
        if self.mtu <= self.length:
            raise IpfixEncodeError("MTU too small: "+str(self.mtu))
    
        # no current set
        self.cursetid = None
        
    def export_new_set(self, setid):
        # close current set if any
        self.export_close_set()

        if setid >= 256:
            # make sure we have a template for the set
            if not ((self.odid, setid) in self.templates):
                raise IpfixEncodeError("can't start set without template id " + 
                                       str(setid))

            # make sure we have room to export at least one record
            tmpl = self.templates[(self.odid, setid)]
            if self.length + _sethdr_st.size + tmpl.minlength > self.mtu:
                raise EndOfMessage()
        else:
            # special Set ID. no template
            tmpl = None
        
        # set up new set
        self.cursetoff = self.length
        self.cursetid = setid
        self.curtmpl = tmpl
        _sethdr_st.pack_into(self.mbuf, self.length, setid, 0)
        self.length += _sethdr_st.size
        
    def export_close_set(self):
        if self.cursetid:
            _sethdr_st.pack_into(self.mbuf, self.cursetoff, 
                                 self.cursetid, self.length - self.cursetoff)
            self.cursetid = None
        
    def export_ensure_set(self, setid):
        if self.cursetid != setid:
            self.export_new_set(setid)

    def export_needs_flush(self):
        if not self.cursetid and self.length <= _msghdr_st.size:
            return False
        else:
            return True
        
    def export_template(self, tmpl):
        self.export_ensure_set(tmpl.native_setid())
        
        if self.length + tmpl.enclength > self.mtu:
            raise EndOfMessage
        
        self.length = tmpl.encode_template_to(self.mbuf, self.length, 
                                              tmpl.native_setid())

    def export_template_withdrawal(self, setid, tid):
        self.export_ensure_set(setid)
        
        if self.length + template.withdrawal_length(setid) > self.mtu:
            raise EndOfMessage
        
        self.length = template.encode_withdrawal_to(self.mbuf, self.length, 
                                                    setid, tid)
    
    def export_all_templates(self):
        pass
    
    def export_record(self, rec, 
                      encode_fn=template.Template.encode_namedict_to, 
                      recinf = None):
        savelength = self.length
        
        try:
            self.length = encode_fn(self.curtmpl, self.mbuf, self.length, rec, recinf)
        except struct.error: # out of bounds on the underlying mbuf 
            self.length = savelength
            raise EndOfMessage()
        
        # check for mtu overrun
        if self.length > self.mtu:
            self.length = savelength
            raise EndOfMessage()

        self._increment_sequence()

    def export_namedict(self, rec):
        self.export_record(rec, template.Template.encode_namedict_to)
    
    def export_tuple(self, rec, ielist = None):
        self.export_record(rec, template.Template.encode_tuple_to, ielist)