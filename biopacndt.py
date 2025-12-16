#!/usr/bin/env python
# encoding: utf-8

"""
biopacndt.py

Implementation of the biopacndt Python module with supporting classes
and functions for simplifying controlling remote AcqKnowledge instances
and process binary data sent by AcqKnowledge to Python code over network 
connections during data acquisitions.

Copyright (c) 2009-2010 BIOPAC Systems, Inc. All rights reserved.
"""

import socket
import xmlrpc.client
import socketserver
import threading
import struct
import time
import inspect
import sys

class AcqNdtChannel:
	""" AcqKnowledge Network Data Transfer Channel
	
	Stores information about a data channel within the active graph within
	AcqKnowledge.  This describes the hardware configuration, the settings
	that will be applied when data is acquired.   It also indicates if
	the channel is enabled for binary streaming or 
	
	This class should not be created or manipulated directly.
	
	AcqNdtServer returns AcqNdtChannel and populates the information. 
	"""

	def __init__(self):
		""" Default constructor
		"""
		
		## Lists the channel type.  One of the following strings:
		##
		## "analog"		for physical analog channels of the MP device
		## "digital"	        for TTL digital input lines of the MP device
		## "calc"		for calculation channels showing derived data
		## "FaceReader"		for FaceReader channels showing emotional states
		##                      received from FaceReader
		self.Type = None
		
		## Index of the channel.  Each individual channel type has an
		## index starting at 0.
		self.Index = None
		
		## Number of bytes occupied by a single sample of binary data
		## delivered over a network connection.
		##
		## For this release of the module, this will always be a fixed
		## value of 8 bytes for all channels.
		self.DataSize = None
		
		## Downsampling divider for the channel.  The hardware
		## acquisition sampling rate may be divided by this value to
		## obtain the channel sampling rate.  A value of 1 indicates
		## the channel is not downsampled and is acquired "full speed".
		##
		## Data is delivered over network connections using the channel
		## sampling rate.  If varying channel sampling rates are used,
		## it is recommended to use the "multiple" communication mode
		## to avoid the need to process frames.
		self.SamplingDivider = None
		
		## Scaling factor for converting channel data.  This is used
		## primarily to convert integer formatted data into actual
		## amplitude data using the formula:
		##
		## v * Scale + Offset
		##
		## For this release of the module, this will always be a fixed
		## value of 1.0 for all channels.  This may change in future
		## module revisions.
		self.Scale = None
		
		## Offset factor for converting channel data.  This is used
		## primarily to convert integer formatted data into actual
		## amplitude data using the formula:
		##
		## v * Scale + Offset
		##
		## For this release of the module, this will always be a fixed
		## value of 0.0 for all channels.  This may change in future
		## module revisions.
		self.Offset = None
		
		## Boolean value indicating if AcqKnowledge will stream the
		## data of this channel over a network connection during
		## acquisition.
		##
		## This must be True for every channel used to construct
		## an AcqNdtDataServer.
		##
		## If this is False, the channel may only be used for 
		## "getMostRecentSampleValue" style XML-RPC calls.  Binary
		## data will not be streamed to the client application.
		self.EnabledForDelivery = None
					
	def GetSimpleChannelStruct(self):
		""" Converts the instance into a simple channel index structure.
		
		This simple structure may be used with XML-RPC control connection calls with an AcqNdtServer instance, such as getMostRecentSampleValue.
				
		Returns a dictionary containing the channel type and index
		"""
		return {"type":self.Type,"index":self.Index}
		
	def __str__(self):
		""" Return printable string representation. """
		return "%s" % self.__dict__
	
	def __repr__(self):
		""" Return printable string representation. """
		return self.__str__()
	
	def __setattr__(self, name, value):
		""" Used to enforce attributes that are read-only and may be set only once.
		
		Only AcqNdtServer instances should return instances of these objects.
		Any of the properties of a channel cannot be changed in local
		objects, but rather must be configured through control connection
		XML-RPC calls.  This is why these attributes are read-only.
		"""
		
		if (name not in self.__dict__) or (self.__dict__[name] is None):
			self.__dict__[name]=value
		else:
			if name == 'EnabledForDelivery':
				# We will allow only our specific AcqNdtServer Deliver function which
				# toggles channel delivery to modify this attribute since
				# it will explicitly sync it with the remote AcqKnowledge
				# application over the control connection.  All other callers
				# will be denied modification privileges.
				#
				# This of course isn't perfect, but is close enough to
				# remind callers of object immutability while adding a
				# 'friend' class.
				
				if sys._getframe(1).f_code.co_name == 'Deliver':
					self.__dict__[name]=value
				else:
					raise ACQException("AcqNdtChannel instances cannot be modified!")
				
			else:
				raise ACQException("AcqNdtChannel instances cannot be modified!")
		

class AcqNdtServer:
	""" Used to control a remote AcqKnowledge application.
	
	This class is used to perform common operations such as sending a
	graph template to AcqKnowledge, specifying which channels of an
	open graph should be delivered to an AcqNdtDataServer, starting
	data acquisitions, and waiting for the data acquisitions to end.
	
	This primarily is used to send configuration information and
	commands to AcqKnowledge.
	
	Attributes and member functions starting with Capital Letters
	are helper functions defined in this module.
	
	Invoking a member function with a lowercase letter will actually
	forward the function call to the control connection of the remote
	AcqKnowledge application.  See the "Control Connections" section of
	the "Network Data Transfer Reference" for descriptions of all of
	these available functions.  These control connection XML-RPC 
	functions may be invoked by their "Method name" as described in the
	documentation, removing the "acq." prefix.  For example, to invoke
	the "acq.toggleAcquisition()" XML-RPC method, you would use
	"AcqNdtServer.toggleAcquisition()" in Python code.
	
	This is meant to be used in conjunction with an instance of the
	AcqNdtDataServer object, which actually receives the data from
	AcqKnowledge.
	
	Internally, these classes use the network data transfer TCP transfer
	protocol with 2 second timeouts and native endian double valued
	channel data delivery.  Code using this class should not modfiy
	any of these transfer settings.
	"""
	
	def __init__(self, host, port):
		""" Constructs an AcqKnowledge Data Transfer server.
		
		Internally, this uses the network data transfer's TCP transfer
		protocol with a timeout of 2 seconds.  These settings should
		
		host: The IP address or host name of a computer running AcqKnowledge.
		port: The port number where the XML-RPC server is listening on, either
		      the value of "Control TCP Port" in AcqKnowledge's Networking
		      preferences, or the port number as returned from an auto-discovery
		      request.
		"""
		
		self.__Host = str(host)
		self.__ControlPort = int(port)
		
		# Configure the list of remote procedure calls available on the
		# remote AcqKnowledge server control connection.  By querying
		# AcqKnowledge for the list of functions it supports, we don't
		# need to rewrite this Python module to support calls added
		# in newer versions of the NDT protocol.
		
		self.__rpcServerURL = "http://%s:%s/RPC2" % (self.__Host, self.__ControlPort)
		
		self.__RPC =  xmlrpc.client.ServerProxy(self.__rpcServerURL)
		
		# we will strip off the leading prefix from all of the XML-RPC method
		# names so we can access them directly as member functions in the
		# AcqNdtServer class.
		self.__acqRPCNamespace = "acq."
		fullList = self.__RPC.system.listMethods()
		#searches the full list of methods, if it starts with the acq namespace remove the acq prefix and add it to the list
		self.__rpcMethods = [m.replace(self.__acqRPCNamespace,"",1) for m in fullList if m.startswith(self.__acqRPCNamespace)]
		
		# Configure our fundamental binary data transfer type.  For ease
		# of moving data into Python, we will always use TCP connections
		# with double precision floating point delivery.
		#
		# Set the tcp mode here;  data type options will be set when the
		# array of available channels is retrieved.
		
		#always use tcp
		self.changeTransportType('tcp')
		#tcp timeout
		self.setDataConnectionTimeoutSec(2)
	
	def LoadTemplate(self, filename):
		""" Sends a graph template file to AcqKnowledge to create a new graph window with its hardware settings.
		
		filename: path to the graph template file on disk that should be transferred to AcqKnowledge 

		CAUTION:  Always remember to use the capitalized "LoadTemplate"!
		The lowercase XML-RPC control connection loadTemplate() function does
		not work with files on disk, only base64 encoded data.  This
		helper function will work directly with files on disk.
		
		CAUTION:  If the file specified is not an AcqKnowledge graph template
		file, sending it may crash AcqKnowledge.
		
		"""
		
		# specify "rb" for cross platform  (http://bugs.python.org/issue1735418)
		fd = open(filename, "rb")
		
		# read arbitrary size file
		data = ""
		while True:
			partial = fd.read(1024)
			data += partial
			
			if "" == partial:
				break
				
		fd.close()
		
		binaryFile = xmlrpc.client.Binary(data)
		
		self.loadTemplate(binaryFile)
		
		# Note that after we load a new graph template file, the graph
		# template itself may contain previously saved network settings.
		# Network settings are stored and saved within graph files.
		#
		# Since our Python data server does require TCP connections and
		# we want to keep data connections open temporarily after the
		# completion of the acquisition to deliver the final data samples,
		# we will always reset the transfer mode and timeout after we
		# load the settings from a new graph template within AcqKnowledge.
		
		#always use tcp
		self.changeTransportType('tcp')
		#tcp timeout
		self.setDataConnectionTimeoutSec(2)

	
	def GetChannels(self, channelType):
		""" Get a list of channels of a specific type set for data acquisition in the front most graph in AcqKnowledge.
		
		Each type of data channel should be retrieved with individual calls
		to this function.  After LoadTemplate() is used, this function should
		be called again to retrieve the new settings and perform background
		initializations needed to use AcqNdtDataServer objects to receive
		their data over the network.
	
		channelType: "analog", "digital", "calc", or "FaceReader
		
		Returns a list of AcqNdtChannel objects describing all channels of the specified type.
		
		To be compatible with the AcqNdtDataServer class, this will configure
		all of the channels to be transferred to our Python code using double
		precision floating point native endian format.  Code using 
		AcqNdtDataServer should *not* change the data type!
		
		"""
		
		aChannels = []
		enabledCH = self.getEnabledChannels(channelType)
		
		for index in enabledCH:
			aCH = AcqNdtChannel()
			aCH.Type = channelType
			aCH.Index = index
		
			aCH.SamplingDivider = self.getDownsamplingDivider(aCH.GetSimpleChannelStruct())
			
			#for simplicity only handle 64 bits native endian channels
			dataTypeStruct = {"type":"double","endian":sys.byteorder};
			self.changeDataType(aCH.GetSimpleChannelStruct(), dataTypeStruct)
			aCH.DataSize = 8 #data type size in bytes 64 bits
			aCH.Scale = 1.0
			aCH.Offset = 0.0
			
			aCH.EnabledForDelivery = self.getDataDeliveryEnabled(aCH.GetSimpleChannelStruct())
			aChannels.append(aCH)
		
		return aChannels
	
	def GetAllChannels(self):
		"""Get a list of all of the data channels enabled for acquisition in the front most graph in AcqKnowledge.
		
 		After LoadTemplate() is used, this function should
		be called again to retrieve the new settings and perform background
		initializations needed to use AcqNdtDataServer objects to receive
		their data over the network.
			
		Returns a list of AcqNdtChannel objects describing all channels of the specified type.
		
		To be compatible with the AcqNdtDataServer class, this will configure
		all of the channels to be transferred to our Python code using double
		precision floating point native endian format.  Code using 
		AcqNdtDataServer should *not* change the data type!
		"""
		
		return self.GetChannels('analog') + self.GetChannels('digital') + self.GetChannels('calc') + self.GetChannels('FaceReader')
	
	def GetChannelLabel(self, acqChannel):
		"""Get the label for a specified channel
		
		acqChannel:  must be of type biopacndt.AcqNdtChannel
		"""
		
		return self.getChannelLabel(acqChannel.GetSimpleChannelStruct())
	
	def GetDataConnectionPort(self, acqChannel):
		"""Get the data connection port for the specified channel.
		
		When multiple TCP connection delivery is being used, this port number
		should be used in the constructor to create an AcqNdtDataServer
		for receiving the data of the channel during acquisitions.
		Use the call:
		
			o.changeDataConnectionMethod('multiple')
		
		to ensure multiple TCP connection protocol for use with per-channel
		ports (where o is an instance of AcqNdtServer).
		
		acqChannel: must be of type biopacndt.AcqNdtChannel
		"""
		return self.getDataConnectionPort(acqChannel.GetSimpleChannelStruct())
		
	def ChangeDataConnectionPort(self, acqChannel, port):
		"""Changes the data connection port for the specified channel.
		
		When multiple TCP connection delivery is being used, this port number
		should be used in the constructor to create an AcqNdtDataServer
		for receiving the data of the channel during acquisitions.
		Use the call:
		
			o.changeDataConnectionMethod('multiple')
		
		to ensure multiple TCP connection protocol for use with per-channel
		ports (where o is an instance of AcqNdtServer).
		
		acqChannel: the channel. it must be of type biopacndt.AcqNdtChannel
		port: a valid TCP port.  Note that this port must not be in use by
		      any other channels or applications.  In general, security
		      restrictions prevent most user accounts from using ports
		      underneath 1024.
		"""
		return self.changeDataConnectionPort(acqChannel.GetSimpleChannelStruct(), port)
		
	def DeliverAllEnabledChannels(self):
		"""Sets all the channels that are enabled for acquisition to be delivered via the Network Data Transfer protocol.
		
		After enabling all channels, code should then construct an appropriate
		AcqNdtDataServer instance, either a single instance for 'single'
		data delivery or one instance for each channel for 'multiple' 
		connection data delivery.
		
		Returns a list of all of the AcqNdtChannel objects enabled for data
		delivery.
		"""
		for c in self.GetAllChannels():
			self.Deliver(c, True)
			
		return self.GetAllChannels()
		
	def Deliver(self, acqChannel, state):
		"""Change whether the data of a specific channel will be delivered by AcqKnowledge to an AcqNdtDataServer.
		
		Not all channels that are acquired necessarily need to be streamed
		by AcqKnowledge.  For example, if an analog channel is used only as 
		a source for a calculation channel and the Python code examines only
		the calculation channel data, turning off the analog channel for
		network data delivery will reduce network and CPU load to allow
		AcqKnowledge to work more efficiently.
		
		If in 'single' connection mode, channels that have their 
		EnabledForDelivery attribute equal to False will not be included in
		each data frame.
		
		If in 'multiple' connection mode, channels that have their 
		EnabledForDelivery attribute equal to False will not have any
		connectinos made to AcqNdtDataServers listening on that channel's
		port.
		
		"""
		
		self.changeDataDeliveryEnabled(acqChannel.GetSimpleChannelStruct(), state)
		acqChannel.EnabledForDelivery = state
		
	def WaitForAcquisitionEnd(self):
		"""Blocks until any data acquisition within AcqKnowledge has completed.
		
		While blocked, all AcqNdtDataServer instances and registered data
		handlers will continue to process data.  Additionally any other
		preemptive threads will continue to run.
		
		Use this instead of writing custom loops calling getAcquisitionInProgress().
		This helper method helps reduce control thread network requests which
		helps AcqKnowledge and network data delivery to function more efficiently.
		"""
		
		while self.getAcquisitionInProgress():
			time.sleep(0.25)
		
		# sleep for an additional 1.5 second here.  when the data acquisition
		# ends within AcqKnowledge, the final samples of data from the
		# acquisition will still be transferred after the acquisition has
		# halted (due to our 'connection timeout' of 2 sec.).
		#
		# by waiting for an additional period, we'll hopefully be able to 
		# catch and process the final bits of data coming in over the
		# network connection.  This will hopefully prevent the majority of
		# cases where Python code using this module starts invoking cleanup
		# functions before all of the data has actually been received.
		
		time.sleep(1.5)
			
	def DispatchedMethodList(self):
		"""Returns a list of all available control channel methods.
		
		These methods are documented in the "Control Connections" section 
		of the Network Data Transfer Reference.
		"""
		
		return self.__rpcMethods 
	
	#dispatched functions
	def __getattr__(self, item):
		"""Redirects undefine attributes to the RPC call.
		
		Used to invoke remote functions for the connected AcqKnowledge 
		application as native python instance methods.
		"""

		# all redirect methods that are actually an rpc method
		if item not in self.__rpcMethods:
			raise AttributeError(item)
			
		try:
			rpc_name = self.__acqRPCNamespace + item
			return self.__RPC.__getattr__(rpc_name)
		except:
			raise AttributeError(item)

			
class AcqNdtChannelRecorder:
	"""Helper class that may be used with an AcqNdtDataServer to stream received channel data into a binary file on disk.
	
	This class keeps additional parameters for the data server callback
	to locate the file on disk and to match specific channels of each frame.
	
	Data will be written to disk in 64 bit native endian double format.
	"""
	
	def __init__(self, filename, channel):
		"""Default constructor.
		
		filename:	absolute path to the file on disk where binary data should
					be kept.  Previous file contents will be overwritten.
		channel:	AcqNdtChannel object of the channel whose data should be
					recorded.
		"""
		
		self.__binFile = open(filename, "wb")
		self.__channel = channel
	
	def __del__(self):
		"""Default destructor.
		
		Closes any open filehandles which will no longer be referenced after
		this object no longer exists.
		"""
		
		if self.__binFile and not self.__binFile.closed:
			self.__binFile.close()

	def Close(self):
		"""Write any remaining data to the file and close open filehadles.
		"""
		self.__binFile.flush()
		self.__binFile.close()

	def Write(self, index, frame, channelsInSlice):
		"""Callback function to handle data as delivered from an AcqNdtDataServer.  Writes data to the file on disk.
		
		index:	hardware sample index of the frame passed to the callback.
				to convert to channel samples, divide by __channel.SamplingDivider
		frame:	a tuple of doubles representing the amplitude of each channel
				at the hardware sample position in index.  The index of the
				amplitude in this tuple matches the index of the corresponding
				AcqNdtChannel structure in channelsInSlice
		channelsInSlice:	a tuple of AcqNdtChannel objects indicating which
				channels were acquired in this frame of data.  The amplitude
				of the sample of the channel is at the corresponding location
				in the frame tuple.
		"""
		
		# if our file has already been closed by a call to Close(), no
		# need to perform any processing
		
		if self.__binFile.closed:
			return
		
		# locate the index into the frame that has the amplitude for our
		# specific channel
		
		frameIndex = 0
		for ch in channelsInSlice:
			if self.__channel.Type != ch.Type and self.__channel.Index != ch.Index:
				frameIndex += 1
			else:
				break

		if frameIndex == len(frame):
			# our channel was not present in the frame.  This is expected if
			# our channel is downsampled and may not have a valid amplitude at
			# each hardware sample index position.
			
			return

		# put call for data storing into Try/Except bracket to hide any possible run-time exception
		try:
			self.__binFile.write(struct.pack("d",frame[frameIndex]))
		except:
			pass

			
class AcqNdtDataServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
	"""Receives binary data from AcqKnowledge during acquisitions using the network data transfer protocol and invokes callbacks to allow clients to process the data.
	
	Prior to starting the acquisition, the AcqNdtDataServer objects must be
	created, callbacks registered using RegisterCallback(), and Start() invoked.
	
	If the delivery method used by the AcqNdtServer is 'single', then only
	one AcqNdtDataServer object should be used.  If the delivery method used
	by the AcqNdtServer is 'multiple', then there should be one AcqNdtDataServer
	object created for each channel of data being received.
	
	After the acquisition is finished, Stop() should be invoked for each
	AcqNdtDataServer to clean up processing and release network resources
	used only during the acquisition.
	"""
	
	def __init__(self, port, channels):
		"""Default constructor.
		
		The arguments supplied to the constructor should vary depending on
		the transfer mode of the AcqNdtServer object.
		
		
		If AcqNdtServer.getDataConnectionMethod()=='single'
		
		port:	set this paramter to AcqNdtServer.getSingleConnectionModePort()
		channels:	set this paramter to a list of all of the AcqNdtChannel objects
					that are set for delivery.  This list may be determined by the
					following list comprehension:
					
					[x for x in AcqNdtServer.GetAllChannels() if x.EnabledForDelivery]
		
		
		If AcqNdtServer.getDataConnection()=='multiple', construct one object
		for a specific AcqNdtChannel c using:
		
		port:	set this paramter to AcqNdtServer.GetDataConneciton(c)
		channels:	set this parameter to a single element list with the channel object, e.g.
		
					[c]
		"""
		
		self.__enabledChannels = channels
		
		self.__callBacks = {}
		self.__closedCallBacks = {}
		self.__collect = True
		
		self.__collectorThread = threading.Thread(target=self.handle_request)
		self.__collectorThread.setDaemon(True)
		
		socketserver.TCPServer.__init__(self, ("",port), self.AcqNdtDataHandler)
		# configure the socket for re-use 
		self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	
	def __del__(self):
		"""Default destrutor.
		
		Make sure that if our socket has not been closed, we perform appropriate
		cleanup to close the socket.
		"""
		
		try:
			self.server_close()
		except:
			pass
	
	def RegisterCallback(self, name, callback):
		"""Register a new callback function to be invoked to process data as it is delivered from AcqKnowledge.
		
		name:		set to a unique identifier that may reference the callback in RemoveCallback
		callback:	set to the function callback.  Callbacks are invoked with
					three parameters and should have a signature
					"f(hardwareIndex, frame, chanInfo)".  These parameters
					are interpreted as follows:
					
					hardwareIndex 	the sample position of data that was acquired
									as measured at the acquisition sampling rate.
									For channels that are downsampled, this
									should be divided by the SamplingDivider to
									convert to a channel-relative sample.
					
					frame			a tuple of amplitude values for channels as
									sampled at this hardware index
					
					channelInfo		a tuple of AcqNdtChannel objects with the
									information corresponding to amplitude at
									the same position within the frame tuple.
									These may be used to determine channel type,
									index, dividers, etc.  They will always
									be elements of the channels array passed
									into the constructor.
		"""
		
		if name in self.__callBacks:
			raise ACQException("Callback name '" + name + "' is already in use")
		
		self.__callBacks[name] = callback
	
	def RemoveCallback(self, name):
		"""Remove a previously registered callback.
		
		name:	unique ID of the callback to be registered.
		"""
		
		if name in self.__callBacks:
			del self.__callBacks[name]
		
	def GetCallbacks(self):
		"""Returns a dictionary of all registered callbacks, key is unique ID name, value is function reference.
		"""
		
		return dict(self.__callBacks) # read only
			
	def RegisterCloseCallback(self, name, callback):
		"""Register a new callback function to be invoked when the socket is closed..
		
		name:		set to a unique identifier that may reference the callback in RemoveCallback
		callback:	set to the function callback.  Callbacks are invoked with
					no parameters and should have a signature
					"f()".
		"""
		
		if name in self.__closedCallBacks:
			raise ACQException("Close callback name '" + name + "' is already in use")
		
		self.__closedCallBacks[name] = callback
	
	def GetCloseCallbacks(self):
		"""Returns a dictionary of all registered close callbacks, key is unique ID name, value is function reference.
		"""
		
		return dict(self.__closedCallBacks)	# read only
	
	def IsCollecting(self):
		"""Returns whether this object is actively waiting to process incoming network data.
		"""
		return self.__collect
	
	def SetCollecting(self, state):
		"""Change whether the object should continue processing incoming network data.
		
		Should only be used by the data handling implementation.
		
		state:	New collecting state, True or False
		"""
		self.__collect = state
		
	def GetEnabledChannels(self):
		"""Return a list of AcqNdtChannel objects whose incoming data is processed by this object.
		"""
		return self.__enabledChannels
	
	def Start(self):
		"""Begin waiting to receive data from AcqKnowledge and processing incoming data.
		
		This should be called prior to starting the data acquisition within AcqKnowledge.
		"""
		self.__collect = True
		self.__collectorThread.start()
		
	def Stop(self):
		"""Stop processing incoming data.
		
		This should be called after data acquisition within AcqKnowledge has halted.
		"""
		self.__collect = False
		self.__collectorThread.join()
		
		# after our collection thread has stopped, also close our server socket.
		# this avoids leaving it bound after the object is finished being
		# used.
		
		try:
			self.socket.shutdown(socket.SHUT_RDWR)
		except:
			pass
		
		try:
			self.server_close()
		except:
			pass
		
	class AcqNdtDataHandler(socketserver.BaseRequestHandler):
		"""Internal implementation class used to handle the incoming data connections.
		
		Should not be used outside of the biopacndt module.
		"""
		
		def handle(self):
			"""Handle incoming data connection request.
			
			When the appropriate incoming data connection is made, it begins continuously
			reading in the binary data delivered over the connection (assumed 64 bit
			floating point native endian) and invokes registered data handling callbacks
			to allow client code to process the data appropraitely.
			"""
			
			enabledChannels = self.server.GetEnabledChannels()
			
			# check if the channels were porperly set
			if len(enabledChannels) == 0:
				self.server.SetCollecting(False)
	
			# use python long to avoid wrap issues.  Long is fine here
			# as it matches the 32 bit maximum limit on hardware samples
			# within AcqKnowledge.
			index = 0
			while self.server.IsCollecting():
				# determine how many bytes to read per frame
				binaryFormat = ""
				channelsInSlice = []
				if len(enabledChannels) == 1:
					channelsInSlice = enabledChannels
				else:
					for ch in enabledChannels:
						if ((index % ch.SamplingDivider) == 0):
							channelsInSlice.append(ch)
	
				binaryFormat = "d"*len(channelsInSlice)
	
				# number of bytes to read #move the comment somewhere else
				recvLen = len(binaryFormat)*8 #assumes 64-bit double
	
				# ignore request for zero bytes
				if 0 != recvLen:
					# read the bytes off the socket
					buf = self.request.recv(recvLen)
					
					# stop error
					if len(buf) != recvLen:
						# invoke the close handlers if AcqKnowledge diconnected
						if len(buf) == 0:
							closeCallbacks = self.server.GetCloseCallbacks()
							for (name, func) in closeCallbacks.items():
								func()
						
						self.server.SetCollecting(False)
					else:
						# convert the buffer into an array of doubles
						frame = struct.unpack(binaryFormat, buf)
						
						# convert the modifiable list of channel info into
						# a tuple to prevent modfiication by callbacks
						
						channelsInSliceTuple = tuple(channelsInSlice)
						
						# call the callback functions
						callbacks = self.server.GetCallbacks()
						for (name, func) in callbacks.items():
							func(index, frame, channelsInSliceTuple)
				
				index += 1

class ACQException(Exception):
	"""Exceptions thrown for various error conditions in the classes contained
	in this module.
	"""
	
	def __init__(self, message):
		"""Default constructor.
		
		message:	text indicating the error condition leading to the exception.
		"""
		Exception.__init__(self, message)
	
def AcqNdtQuickConnect():
	"""Helper function for quickly obtaining a server object for a running AcqKnowledge instance.
	
	This will return an AcqNdtServer object either for the localhost computer or,
	if AcqKnowledge is not running on the local computer, the first comptuer
	found on the network where AcqKnowledge is running with networking functionality
	enabled.
	
	If you want to connect to a specific machine by IP address, construct
	the AcqNdtServer object manually instead.
	"""
	
	serverList = FindAcqNdtServers()
	
	if len(serverList) < 1:
		raise ACQException("No AcqKnowledge Servers Found")
	
	hostname, port = serverList[0]
	
	return AcqNdtServer(hostname, port)

def FindAcqNdtServers():
	"""Locates any comptuers on the network where AcqKnowledge is running with
	the networking feature enabled and with the "Respond to auto-discovery requests"
	Networking preference enabled.
	
	return:  list of tuples, one tuple for each located server.  first tuple 
			 element is an IPV4 address, second tuple element is the 'control
			 conection port' that should be passed to the AcqNdtServer constructor.
	
	If AcqKnowledge is running on the current machine, the current machine
	information will always be returned as the first tuple in the list.
	
	If auto-discovery is disabled within the Networking preferences, this
	function will not be able to find that specific computer and its IP address
	and control port information will need to be hardcoded to connect to it
	with the AcqNdtServer constructor.
	"""
	
	# assemble and send out our broadcast packet to contact any running
	# AcqKnowledge applications.
	
	sendPort = 15012
	sendMsg = "AcqP Client"
	hostPort = ('255.255.255.255', sendPort)
	
	finderSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	finderSock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
	finderSock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	finderSock.settimeout(0.25)	# only wait a quarter second for responses
	
	sendMsgBytes = str.encode(sendMsg) #required as of Python 3.5
	
	messagePrefix = "AcqP Server Port"
	finderSock.sendto(sendMsgBytes, hostPort)
	
	# loop for the timeout period specified above to receive responses
	# from as many AcqKnowledge instances as possible.
	
	acqServerPort = []
	try:
		while True:
			(recvMsg, (ipAddr,port)) = finderSock.recvfrom(512)
			recvMsg = recvMsg.decode()#required as of Python 3.5
			if recvMsg.startswith(messagePrefix):
				tokens = recvMsg.split(":")
				if len(tokens) == 2:
					# make sure our IP address does not already appear in the list
					
					for x in acqServerPort:
						if x[0] == ipAddr:
							break
					else:
						# we didn't find it in any of our tuples, so add it to the list
						acqServerPort.append( (ipAddr, tokens[1]) )
	except socket.timeout:
		pass #  we just timed out
	
	# networks may contain multiple computers running AcqKnowledge
	# that can respond to this request.  A common scenario is to
	# run applications on the same computer as AcqKnowledge.  To
	# facilitate this, if the localhost responded we will move it
	# to the front of the list.  This will allow the first element
	# to always be used to refer either to the current computer 
	# otherwise the first available computer.
	
	try:
		# we will search only for IPv4 addresses as our socket code is
		# not IPv6 compatible
		myIP = [x for x in ( socket.getaddrinfo(socket.gethostname(), None) ) if x[0] == socket.AF_INET][0][4][0]
	except socket.gaierror:
		myIP = None
	except IndexError:
		myIP = None
		
	for i in range(len(acqServerPort)):
		ipAddr, port = acqServerPort[i]
		if (ipAddr == '127.0.0.1') or ( myIP and ( ipAddr == myIP ) ):
			acqServerPort[i], acqServerPort[0] = acqServerPort[0], acqServerPort[i]
			break
	
	return acqServerPort

def main():
	"""Basic self test code to connect to the first located AcqKnowledge
	server and print out its XML-RPC method list.
	"""
	
	print("Self test start")
	print("Locating AcqKnowledge NDT Servers...")
	serverList = FindAcqNdtServers()
	
	if len(serverList) < 1:
		print("No AcqKnowledge Servers Found")
		return
	
	hostname, port = serverList[0]
	print("Connecting to %s at port %s" % serverList[0])
	acqServer = AcqNdtServer(hostname, port)
	methodList = acqServer.DispatchedMethodList()
	
	print("Dispatched Method List:")
	print("---")
	for	method in methodList:
		print(method)
	print("---")
	
	print("Acq Server [%s:%s]" % (hostname, port))
	print("MP Device Type: %s" % acqServer.getMPUnitType())
	print("Self test complete.")

if __name__ == '__main__':
	main()

