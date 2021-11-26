
from ctypes import POINTER
from aardwolf.authentication import credssp
import traceback
import asyncio
import typing
from typing import cast
from collections import OrderedDict

import asn1tools
from aardwolf import logger
from aardwolf.commons.iosettings import RDPIOSettings
from aardwolf.network.selector import NetworkSelector
from aardwolf.commons.credential import RDPCredentialsSecretType, RDPAuthProtocol
from aardwolf.commons.cryptolayer import RDPCryptoLayer
from aardwolf.protocol import x224
from aardwolf.protocol.T124.userdata.serversecuritydata import TS_UD_SC_SEC1
from aardwolf.transport.ssl import SSLClientTunnel
from aardwolf.network.tpkt import TPKTNetwork
from aardwolf.network.x224 import X224Network

from aardwolf.protocol.x224.constants import SUPP_PROTOCOLS, NEG_FLAGS
from aardwolf.protocol.x224.server.connectionconfirm import RDP_NEG_RSP

from aardwolf.protocol.T124.GCCPDU import GCCPDU
from aardwolf.protocol.T124.userdata import TS_UD, TS_SC
from aardwolf.protocol.T124.userdata.constants import *
from aardwolf.protocol.T124.userdata.clientcoredata import TS_UD_CS_CORE
from aardwolf.protocol.T124.userdata.clientsecuritydata import TS_UD_CS_SEC
from aardwolf.protocol.T124.userdata.clientnetworkdata import TS_UD_CS_NET, CHANNEL_DEF
from aardwolf.protocol.T124.userdata.clientclusterdata import TS_UD_CS_CLUSTER
from aardwolf.protocol.T128.security import TS_SECURITY_HEADER,SEC_HDR_FLAG, TS_SECURITY_HEADER1
from aardwolf.protocol.T125.infopacket import *
from aardwolf.protocol.T125.extendedinfopacket import *
from aardwolf.protocol.T125.MCSPDU_ver_2 import MCSPDU_ver_2
from aardwolf.protocol.T125.serverdemandactivepdu import *
from aardwolf.protocol.T125.clientconfirmactivepdu import *
from aardwolf.protocol.T125.synchronizepdu import *
from aardwolf.protocol.T125.controlpdu import *
from aardwolf.protocol.T125.fontlistpdu import *
from aardwolf.protocol.T125.inputeventpdu import *
from aardwolf.protocol.T125.securityexchangepdu import TS_SECURITY_PACKET
from aardwolf.protocol.T125.seterrorinfopdu import TS_SET_ERROR_INFO_PDU


from aardwolf.protocol.fastpath import TS_FP_UPDATE_PDU, FASTPATH_UPDATETYPE, FASTPATH_FRAGMENT, FASTPATH_SEC, TS_FP_UPDATE
from aardwolf.commons.queuedata import *
from aardwolf.commons.authbuilder import AuthenticatorBuilder
from aardwolf.channels import Channel
from aardwolf.extensions.RDPECLIP.channel import RDPECLIPChannel

class RDPConnection:
	def __init__(self, target, credentials, iosettings:RDPIOSettings, authapi = None, channels = [RDPECLIPChannel]):
		self.target = target
		self.credentials = credentials
		self.authapi = authapi
		self.iosettings = iosettings

		# these are the main queues with which you can communicate with the server
		# ext_out_queue: yields video data
		# ext_in_queue: expects keyboard/mouse data
		self.ext_out_queue = asyncio.Queue()
		self.ext_in_queue = asyncio.Queue()


		self.__tpkgnet = None
		self._x224net = None
		self.__transportnet = None #TCP/SSL/SOCKS etc.
		self.__t125_ber_codec = None
		self._t125_per_codec = None
		self.__t124_codec = None

		self.x224_connection_reply = None
		self.x224_protocol = None

		self.__server_connect_pdu:TS_SC = None # serverconnectpdu message from server (holds security exchange data)
		
		self._initiator = None
		self.__channel_id_lookup = {}
		self.__joined_channels =  OrderedDict({})
		
		for channel in channels:
			self.__joined_channels[channel.name] = channel(self.iosettings)
		
		self.__channel_task = {} #name -> channeltask

		
		self.__fastpath_reader_task = None
		self.__external_reader_task = None
		self.__x224_reader_task = None
		self.client_x224_flags = 0
		self.client_x224_supported_protocols = SUPP_PROTOCOLS.SSL |SUPP_PROTOCOLS.HYBRID_EX
		self.cryptolayer:RDPCryptoLayer = None

	
	async def terminate(self):
		try:
			for name in self.__joined_channels:
				await self.__joined_channels[name].disconnect()
			
			if self.ext_out_queue is not None:
				await self.ext_out_queue.put(None)
				
			
			if self.__external_reader_task is not None:
				self.__external_reader_task.cancel()
			
			if self.__fastpath_reader_task is not None:
				self.__fastpath_reader_task.cancel()
			
			if self.__x224_reader_task is not None:
				self.__x224_reader_task.cancel()
			
			if self._x224net is not None:
				await self._x224net.disconnect()

			if self.__tpkgnet is not None:
				await self.__tpkgnet.disconnect()
			
			return True, None
		except Exception as e:
			traceback.print_exc()
			return None, e
	
	async def __aenter__(self):
		return self
		
	async def __aexit__(self, exc_type, exc, traceback):
		await asyncio.wait_for(self.terminate(), timeout = 5)
	
	async def connect(self):
		"""
		Performs the entire connection sequence 
		"""
		try:

			self.__transportnet, err = await NetworkSelector.select(self.target)
			if err is not None:
				raise err

			# starting lower-layer transports 
			_, err = await self.__transportnet.connect()
			if err is not None:
				raise err

			# TPKT network handles both TPKT and FASTPATH packets
			# This object is also capable to dynamically switch 
			# to SSL/TLS when needed (without reconnecting)
			self.__tpkgnet = TPKTNetwork(self.__transportnet)
			_, err = await self.__tpkgnet.run()
			if err is not None:
				raise err
			

			self.__fastpath_reader_task = asyncio.create_task(self.__fastpath_reader())

			# X224 channel is on top of TPKT, performs the initial negotiation
			# between the server and our client (restricted admin mode, authentication methods etc)
			# are set here
			self._x224net = X224Network(self.__tpkgnet)
			_, err = await self._x224net.run()
			if err is not None:
				raise err
			
			if self.credentials is not None:
				if self.credentials.secret is not None and self.credentials.secret_type not in [RDPCredentialsSecretType.PASSWORD, RDPCredentialsSecretType.PWPROMPT, RDPCredentialsSecretType.PWHEX, RDPCredentialsSecretType.PWB64]:
					# user provided some secret but it's not a password
					# here we request restricted admin mode
					self.client_x224_flags = NEG_FLAGS.RESTRICTED_ADMIN_MODE_REQUIRED
					self.client_x224_supported_protocols = SUPP_PROTOCOLS.SSL |SUPP_PROTOCOLS.HYBRID
				elif self.credentials.secret is None and self.credentials.username is None:
					# not sending any passwords, hoping HYBRID is not required
					self.client_x224_flags = 0
					self.client_x224_supported_protocols = SUPP_PROTOCOLS.SSL
					
			connection_accepted_reply, err = await self._x224net.client_negotiate(self.client_x224_flags, self.client_x224_supported_protocols)
			if err is not None:
				raise err
			
			if connection_accepted_reply.rdpNegData is not None:
				# newer RDP protocol was selected

				self.x224_connection_reply = typing.cast(RDP_NEG_RSP, connection_accepted_reply.rdpNegData)
				# if the server requires SSL/TLS connection as indicated in the 'selectedProtocol' flags
				# we switch here. SSL and HYBRID/HYBRID_EX authentication methods all require this switch
				
				
				self.x224_protocol = self.x224_connection_reply.selectedProtocol
				self.x224_flag = self.x224_connection_reply.flags
				#print(self.x224_protocol)
				#print(self.x224_flag)
				if SUPP_PROTOCOLS.SSL in self.x224_protocol or SUPP_PROTOCOLS.HYBRID in self.x224_protocol or SUPP_PROTOCOLS.HYBRID_EX in self.x224_protocol:
					_, err = await self.__tpkgnet.switch_transport(SSLClientTunnel)
					if err is not None:
						raise err

				# if the server expects HYBRID/HYBRID_EX authentication we do that here
				# This is basically credSSP
				if SUPP_PROTOCOLS.HYBRID in self.x224_protocol or SUPP_PROTOCOLS.HYBRID_EX in self.x224_protocol:
					_, err = await self.credssp_auth()
					if err is not None:
						raise err

			else:
				# old RDP protocol is used
				self.x224_protocol = SUPP_PROTOCOLS.RDP
				self.x224_flag = None

			# initializing the parsers here otherwise they'd waste time on connections that did not get to this point
			# not kidding, this takes ages
			self.__t125_ber_codec = asn1tools.compile_string(MCSPDU_ver_2, 'ber')
			self._t125_per_codec = asn1tools.compile_string(MCSPDU_ver_2, 'per')
			self.__t124_codec = asn1tools.compile_string(GCCPDU, 'per')

			# All steps below are required as stated in the following 'documentation'
			# https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/1d263f84-6153-4a16-b329-8770be364e1b
			_, err = await self.__establish_channels()
			if err is not None:
				raise err
			logger.debug('Establish channels OK')
			
			_, err = await self.__erect_domain()
			if err is not None:
				raise err
			logger.debug('Erect domain OK')
			
			_, err = await self.__attach_user()
			if err is not None:
				raise err
			logger.debug('Attach user OK')
			
			_, err = await self.__join_channels()
			if err is not None:
				raise err
			logger.debug('Join channels OK')
			
			if self.x224_protocol == SUPP_PROTOCOLS.RDP:
				# key exchange here because we use old version of the protocol
				_, err = await self.__security_exchange()
				if err is not None:
					raise err
				logger.debug('Security exchange OK')

			_, err = await self.__send_userdata()
			if err is not None:
				raise err
			logger.debug('Send userdata OK')

			_, err = await self.__handle_license()
			if err is not None:
				raise err
			logger.debug('handle license OK')

			_, err = await self.__handle_mandatory_capability_exchange()
			if err is not None:
				raise err
			logger.debug('mandatory capability exchange OK')

			self.__external_reader_task = asyncio.create_task(self.__external_reader())
			logger.debug('RDP connection sequence done')
			return True, None
		except Exception as e:
			return None, e
	
	async def credssp_auth(self):
		try:
			#constructing authentication API is not specified
			if self.authapi is None:
				if self.credentials is None:
					raise Exception('No auth API nor credentials were supplied!')
				
				self.authapi = AuthenticatorBuilder.to_credssp(self.credentials, self.target)
			# credSSP authentication exchange happens on the 'wire' directly 
			# without the use of TPKT or X224 so we have to suspend those layers
			_, err = await self.__tpkgnet.suspend_read()
			if err is not None:
				raise err

			# credSSP auth requires knowledge of the server's public key 
			pubkey = await self.__tpkgnet.transport.get_server_pubkey()

			# credSSP auth happends here
			token = None
			data, to_continue, err = await self.authapi.authenticate(token, flags = None, pubkey = pubkey)
			if err is not None:
				raise err

			await self.__tpkgnet.transport.out_queue.put(data)
			
			for _ in range(10):
				token, err = await self.__tpkgnet.transport.in_queue.get()
				if err is not None:
					raise err

				data, to_continue, err = await self.authapi.authenticate(token, flags = None, pubkey = pubkey)
				if err is not None:
					raise err
				
				if to_continue is False:
					# credSSP auth finished, flushing remaining data
					if data is not None:
						await self.__tpkgnet.transport.out_queue.put(data)
					
					# if HYBRID_EX auth was selected by the server, the server MUST send
					# an extra packet informing us if the credSSP auth was successful or not
					if SUPP_PROTOCOLS.HYBRID_EX in self.x224_protocol:
						authresult_raw, err = await self.__tpkgnet.transport.in_queue.get()
						if err is not None:
							raise err
						
						authresult = int.from_bytes(authresult_raw, byteorder='little', signed=False)
						#print('Early User Authorization Result PDU %s' % authresult)
						if authresult == 5:
							raise Exception('Authentication failed! (early user auth)')


					
					_, err = await self.__tpkgnet.conitnue_read()
					if err is not None:
						raise err
					return True, None
				
				await self.__tpkgnet.transport.out_queue.put(data)

			_, err = await self.__tpkgnet.conitnue_read()
			if err is not None:
				raise err

		except Exception as e:
			return None, e

	async def __establish_channels(self):
		try:
			ts_ud = TS_UD()

			ud_core = TS_UD_CS_CORE()
			ud_core.desktopWidth = self.iosettings.video_width
			ud_core.desktopHeight = self.iosettings.video_height
			# this part doesn matter since we also set postBeta2ColorDepth
			#ud_core.colorDepth = COLOR_DEPTH.COLOR_8BPP
			if self.iosettings.video_bpp_min == 4:
				ud_core.colorDepth = COLOR_DEPTH.COLOR_4BPP
			elif self.iosettings.video_bpp_min == 8:
				ud_core.colorDepth = COLOR_DEPTH.COLOR_8BPP
			elif self.iosettings.video_bpp_min == 15:
				ud_core.colorDepth = COLOR_DEPTH.COLOR_16BPP_555
			elif self.iosettings.video_bpp_min == 16:
				ud_core.colorDepth = COLOR_DEPTH.COLOR_16BPP_565
			elif self.iosettings.video_bpp_min == 24:
				ud_core.colorDepth = COLOR_DEPTH.COLOR_24BPP
			# from here on it matters

			ud_core.keyboardLayout = self.iosettings.keyboard_layout
			ud_core.clientBuild = 2600
			ud_core.clientName = 'aardworlf'
			ud_core.imeFileName = ''
			#ud_core.postBeta2ColorDepth = COLOR_DEPTH.COLOR_8BPP
			if self.iosettings.video_bpp_min == 4:
				ud_core.postBeta2ColorDepth = COLOR_DEPTH.COLOR_4BPP
			elif self.iosettings.video_bpp_min == 8:
				ud_core.postBeta2ColorDepth = COLOR_DEPTH.COLOR_8BPP
			elif self.iosettings.video_bpp_min == 15:
				ud_core.postBeta2ColorDepth = COLOR_DEPTH.COLOR_16BPP_555
			elif self.iosettings.video_bpp_min == 16:
				ud_core.postBeta2ColorDepth = COLOR_DEPTH.COLOR_16BPP_565
			elif self.iosettings.video_bpp_min == 24:
				ud_core.postBeta2ColorDepth = COLOR_DEPTH.COLOR_24BPP

			ud_core.clientProductId = 1
			ud_core.serialNumber = 0
			ud_core.highColorDepth = HIGH_COLOR_DEPTH.HIGH_COLOR_16BPP

			if self.iosettings.video_bpp_max == 4:
				ud_core.highColorDepth = HIGH_COLOR_DEPTH.HIGH_COLOR_4BPP
			elif self.iosettings.video_bpp_max == 8:
				ud_core.highColorDepth = HIGH_COLOR_DEPTH.HIGH_COLOR_8BPP
			elif self.iosettings.video_bpp_max == 15:
				ud_core.highColorDepth = HIGH_COLOR_DEPTH.HIGH_COLOR_15BPP
			elif self.iosettings.video_bpp_max == 16:
				ud_core.highColorDepth = HIGH_COLOR_DEPTH.HIGH_COLOR_16BPP
			elif self.iosettings.video_bpp_max == 24:
				ud_core.highColorDepth = HIGH_COLOR_DEPTH.HIGH_COLOR_24BPP

			self.iosettings.video_bpp_supported.append(self.iosettings.video_bpp_max)
			self.iosettings.video_bpp_supported.append(self.iosettings.video_bpp_min)
			ud_core.supportedColorDepths = SUPPORTED_COLOR_DEPTH.RNS_UD_16BPP_SUPPORT
			for sc in self.iosettings.video_bpp_supported:
				if sc == 15:
					ud_core.supportedColorDepths |= SUPPORTED_COLOR_DEPTH.RNS_UD_15BPP_SUPPORT
				elif sc == 16:
					ud_core.supportedColorDepths |= SUPPORTED_COLOR_DEPTH.RNS_UD_16BPP_SUPPORT
				elif sc == 24:
					ud_core.supportedColorDepths |= SUPPORTED_COLOR_DEPTH.RNS_UD_24BPP_SUPPORT
				elif sc == 32:
					ud_core.supportedColorDepths |= SUPPORTED_COLOR_DEPTH.RNS_UD_32BPP_SUPPORT
			
			ud_core.earlyCapabilityFlags = RNS_UD_CS.SUPPORT_ERRINFO_PDU
			ud_core.clientDigProductId = b'\x00' * 64
			ud_core.connectionType = CONNECTION_TYPE.UNK
			ud_core.pad1octet = b'\x00'
			ud_core.serverSelectedProtocol = self.x224_protocol
			
			ud_sec = TS_UD_CS_SEC()
			ud_sec.encryptionMethods = ENCRYPTION_FLAG.FRENCH if self.x224_protocol is not SUPP_PROTOCOLS.RDP else ENCRYPTION_FLAG.BIT_128
			ud_sec.extEncryptionMethods = ENCRYPTION_FLAG.FRENCH

			ud_clust = TS_UD_CS_CLUSTER()
			ud_clust.RedirectedSessionID = 0
			ud_clust.Flags = 8|4|ClusterInfo.REDIRECTION_SUPPORTED

			ud_net = TS_UD_CS_NET()
			
			for name in self.__joined_channels:
				cd = CHANNEL_DEF()
				cd.name = name
				cd.options = self.__joined_channels[name].options
				ud_net.channelDefArray.append(cd)
			

			ts_ud.userdata = {
				TS_UD_TYPE.CS_CORE : ud_core,
				TS_UD_TYPE.CS_SECURITY : ud_sec,
				TS_UD_TYPE.CS_CLUSTER : ud_clust,
				TS_UD_TYPE.CS_NET : ud_net
			}

			userdata_wrapped = {
				'conferenceName': {
					'numeric': '0'
				}, 
				'lockedConference': False, 
				'listedConference': False, 
				'conductibleConference': False, 
				'terminationMethod': 'automatic', 
				'userData': [
					{
						'key': ('h221NonStandard', b'Duca'), 
						'value': ts_ud.to_bytes()
					}
				]
			}

			ConnectGCCPDU = self.__t124_codec.encode('ConnectGCCPDU', ('conferenceCreateRequest', userdata_wrapped))
			t124_wrapper = {
				't124Identifier': ('object', '0.0.20.124.0.1'), 
				'connectPDU': ConnectGCCPDU
			}
			t124_wrapper = self.__t124_codec.encode('ConnectData', t124_wrapper)

			initialconnect = {
				'callingDomainSelector': b'\x01', 
				'calledDomainSelector': b'\x01', 
				'upwardFlag': True, 
				'targetParameters': {
					'maxChannelIds': 34, 
					'maxUserIds': 2, 
					'maxTokenIds': 0, 
					'numPriorities': 1, 
					'minThroughput': 0, 
					'maxHeight': 1, 
					'maxMCSPDUsize': -1, 
					'protocolVersion': 2
				}, 
				'minimumParameters': {
					'maxChannelIds': 1, 
					'maxUserIds': 1, 
					'maxTokenIds': 1, 
					'numPriorities': 1, 
					'minThroughput': 0, 
					'maxHeight': 1, 
					'maxMCSPDUsize': 1056, 
					'protocolVersion': 2
				}, 
				'maximumParameters': {
					'maxChannelIds': -1, 
					'maxUserIds': -1001, 
					'maxTokenIds': -1, 
					'numPriorities': 1, 
					'minThroughput': 0, 
					'maxHeight': 1, 
					'maxMCSPDUsize': -1, 
					'protocolVersion': 2
				}, 
				'userData': t124_wrapper
			}

			conf_create_req = self.__t125_ber_codec.encode('ConnectMCSPDU',('connect-initial', initialconnect))
			conf_create_req = bytes(conf_create_req)
			#print(conf_create_req)

			await self._x224net.out_queue.put(conf_create_req)
			response_raw, err = await self._x224net.in_queue.get()
			if err is not None:
				raise err
			server_res_raw = response_raw.data
			server_res_t125 = self.__t125_ber_codec.decode('ConnectMCSPDU', server_res_raw)
			#print(server_res_t125)
			if server_res_t125[0] != 'connect-response':
				raise Exception('Unexpected response! %s' % server_res_t125)
			if server_res_t125[1]['result'] != 'rt-successful':
				raise Exception('Server returned error! %s' % server_res_t125)
			
			server_res_t124 = self.__t124_codec.decode('ConnectData', server_res_t125[1]['userData'])
			if server_res_t124['t124Identifier'][1] != '0.0.20.124.0.1':
				raise Exception('Unexpected T124 response: %s' % server_res_t124)
			
			# this is strange, and it seems wireshark struggles here as well. 
			# it seems the encoding used does not account for all the packet 
			# bytes at the end but those are also needed for decoding the sub-strucutres?!

			data = server_res_t124['connectPDU']
			m = server_res_raw.find(data)
			remdata = server_res_raw[m+len(data):]

			# weirdness ends here... FOR NOW!

			server_connect_pdu_raw = self.__t124_codec.decode('ConnectGCCPDU', server_res_t124['connectPDU']+remdata)
			self.__server_connect_pdu = TS_SC.from_bytes(server_connect_pdu_raw[1]['userData'][0]['value']).serverdata

			# populating channels
			scnet = self.__server_connect_pdu[TS_UD_TYPE.SC_NET]
			for i, name in enumerate(self.__joined_channels):
				self.__joined_channels[name].channel_id = scnet.channelIdArray[i]
				self.__channel_id_lookup[scnet.channelIdArray[i]] = self.__joined_channels[name]

			self.__joined_channels['MCS'] = Channel('MCS') #TODO: options?
			self.__joined_channels['MCS'].channel_id = scnet.MCSChannelId
			self.__channel_id_lookup[scnet.MCSChannelId] = self.__joined_channels['MCS']

			return True, None
		except Exception as e:
			traceback.print_exc()
			return None, e

	async def __erect_domain(self):
		try:
			# the parser could not decode nor encode this data correctly.
			# therefore we are sending these as bytes. it's static 
			# (even according to docu)
			await self._x224net.out_queue.put(bytes.fromhex('0400010001'))
			return True, None
		except Exception as e:
			return None, e
	
	async def __attach_user(self):
		try:
			await self._x224net.out_queue.put(bytes.fromhex('28'))
			response, err = await self._x224net.in_queue.get()
			if err is not None:
				raise err
			response_parsed = self._t125_per_codec.decode('DomainMCSPDU', response.data)
			if response_parsed[0] != 'attachUserConfirm':
				raise Exception('Unexpected response! %s' % response_parsed)
			if response_parsed[1]['result'] != 'rt-successful':
				raise Exception('Server returned error! %s' % response_parsed)
			self._initiator = response_parsed[1]['initiator']
			
			return True, None
		except Exception as e:
			return None, e
	
	async def __join_channels(self):
		try:
			for name in self.__joined_channels:
				joindata = self._t125_per_codec.encode('DomainMCSPDU', ('channelJoinRequest', {'initiator': self._initiator, 'channelId': self.__joined_channels[name].channel_id}))
				await self._x224net.out_queue.put(bytes(joindata))
				response, err = await self._x224net.in_queue.get()
				if err is not None:
					raise err
				
				x = self._t125_per_codec.decode('DomainMCSPDU', response.data)
				if x[0] != 'channelJoinConfirm':
					raise Exception('Could not join channel "%s". Reason: %s' % (name, x))
				
				self.__channel_task[name] = asyncio.create_task(self.__joined_channels[name].run(self))
				
			
			self.__x224_reader_task = asyncio.create_task(self.__x224_reader())
			return True, None
		except Exception as e:
			return None, e
	
	async def __security_exchange(self):
		try:
			self.cryptolayer = RDPCryptoLayer(self.__server_connect_pdu[TS_UD_TYPE.SC_SECURITY].serverRandom)
			enc_secret = self.__server_connect_pdu[TS_UD_TYPE.SC_SECURITY].serverCertificate.encrypt(self.cryptolayer.ClientRandom)
			secexchange = TS_SECURITY_PACKET()
			secexchange.encryptedClientRandom = enc_secret

			sec_hdr = TS_SECURITY_HEADER()
			sec_hdr.flags = SEC_HDR_FLAG.EXCHANGE_PKT
			sec_hdr.flagsHi = 0

			await self.__joined_channels['MCS'].data_in_queue.put((secexchange, sec_hdr, None, None))
			return True, None
		except Exception as e:
			return None, e

	async def __send_userdata(self):
		try:
			systime = TS_SYSTEMTIME()
			systime.wYear = 0
			systime.wMonth = 10
			systime.wDayOfWeek = 0
			systime.wDay = 5
			systime.wHour = 3
			systime.wMinute = 0
			systime.wSecond = 0
			systime.wMilliseconds = 0

			systz = TS_TIME_ZONE_INFORMATION()
			systz.Bias = 4294967236
			systz.StandardName = b'G\x00T\x00B\x00,\x00 \x00s\x00o\x00m\x00m\x00a\x00r\x00t\x00i\x00d\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
			systz.StandardDate = systime
			systz.StandardBias = 0
			systz.DaylightName = b'G\x00T\x00B\x00,\x00 \x00s\x00o\x00m\x00m\x00a\x00r\x00t\x00i\x00d\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
			systz.DaylightDate = systime
			systz.DaylightBias = 4294967236

			extinfo = TS_EXTENDED_INFO_PACKET()
			extinfo.clientAddressFamily = CLI_AF.AF_INET
			extinfo.clientAddress = '10.10.10.101'
			extinfo.clientDir = 'C:\\WINNT\\System32\\mstscax.dll'
			extinfo.clientTimeZone = systz
			extinfo.clientSessionId = 0
			#extinfo.performanceFlags = PERF.DISABLE_WALLPAPER | PERF.DISABLE_THEMING | PERF.DISABLE_CURSORSETTINGS | PERF.DISABLE_MENUANIMATIONS | PERF.DISABLE_FULLWINDOWDRAG

			info = TS_INFO_PACKET()
			info.CodePage = 0
			info.flags = INFO_FLAG.ENABLEWINDOWSKEY|INFO_FLAG.MAXIMIZESHELL|INFO_FLAG.UNICODE|INFO_FLAG.DISABLECTRLALTDEL|INFO_FLAG.MOUSE
			info.Domain = ''
			info.UserName = ''
			info.Password = ''
			if self.authapi is None or SUPP_PROTOCOLS.SSL in self.x224_protocol:
				if self.credentials.domain is not None:
					info.Domain = self.credentials.domain
				if self.credentials.username is not None:
					info.UserName = self.credentials.username
				if self.credentials.secret is not None:
					info.Password = self.credentials.secret
			info.AlternateShell = '' 
			info.WorkingDir = ''
			info.extrainfo = extinfo

			sec_hdr = TS_SECURITY_HEADER()
			sec_hdr.flags = SEC_HDR_FLAG.INFO_PKT
			if self.cryptolayer is not None:
				sec_hdr.flags |= SEC_HDR_FLAG.ENCRYPT
			sec_hdr.flagsHi = 0

			await self.__joined_channels['MCS'].data_in_queue.put((info, sec_hdr, None, None))

			### ENCTEST!!!!
			#response, err = await self._x224net.in_queue.get()
			#if err is not None:
			#	raise err
			#response_parsed = self._t125_per_codec.decode('DomainMCSPDU', response.data)
			#if response_parsed[0] != 'sendDataIndication':
			#	print('WWWAAAAAAA')
			#print(response_parsed[1]['userData'])
			#sec_hdr = TS_SECURITY_HEADER.from_bytes(response_parsed[1]['userData'])
			#print(sec_hdr)
			#if SEC_HDR_FLAG.ENCRYPT in sec_hdr.flags:
			#	dec = self.cryptolayer.client_dec(response_parsed[1]['userData'][12:])
			#	print(dec)
			#	dec = self.cryptolayer.client_enc(response_parsed[1]['userData'][12:])
			#	print(dec)
			#	dec = self.cryptolayer.server_enc(response_parsed[1]['userData'][12:])
			#	print(dec)
			#	dec = self.cryptolayer.server_dec(response_parsed[1]['userData'][12:])
			#	print(dec)


			return True, None
		except Exception as e:
			return None, e

	async def __handle_license(self):
		try:
			# TODO: implement properly
			# https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/7d941d0d-d482-41c5-b728-538faa3efb31
			data, err = await self.__joined_channels['MCS'].out_queue.get()
			if err is not None:
				raise err
			
			res = self._t125_per_codec.decode('DomainMCSPDU', data)
			#print('license ok')

			return True, None
		except Exception as e:
			traceback.print_exc()
			return None, e
	
	async def __handle_mandatory_capability_exchange(self):
		try:
			# waiting for server to demand active pdu and inside send its capabilities
			data, err = await self.__joined_channels['MCS'].out_queue.get()
			if err is not None:
				raise err

			#print(data)
			res = TS_DEMAND_ACTIVE_PDU.from_bytes(data)
			for cap in res.capabilitySets:
				#print(cap)
				if cap.capabilitySetType == CAPSTYPE.GENERAL:
					cap = typing.cast(TS_GENERAL_CAPABILITYSET, cap.capability)
					if EXTRAFLAG.ENC_SALTED_CHECKSUM in cap.extraFlags and self.cryptolayer is not None:
						self.cryptolayer.use_encrypted_mac = True
			#print(res)
			#print('================================== SERVER IN ENDS HERE ================================================')
			
			caps = []
			# now we send our capabilities
			cap = TS_GENERAL_CAPABILITYSET()
			cap.osMajorType = OSMAJORTYPE.WINDOWS
			cap.osMinorType = OSMINORTYPE.WINDOWS_NT
			cap.extraFlags =  EXTRAFLAG.FASTPATH_OUTPUT_SUPPORTED | EXTRAFLAG.NO_BITMAP_COMPRESSION_HDR | EXTRAFLAG.LONG_CREDENTIALS_SUPPORTED
			if self.cryptolayer is not None and self.cryptolayer.use_encrypted_mac is True:
				cap.extraFlags |= EXTRAFLAG.ENC_SALTED_CHECKSUM
			caps.append(cap)

			cap = TS_BITMAP_CAPABILITYSET()
			cap.preferredBitsPerPixel = self.iosettings.video_bpp_max
			cap.desktopWidth = self.iosettings.video_width
			cap.desktopHeight = self.iosettings.video_height
			caps.append(cap)

			#TS_FONT_CAPABILITYSET missing

			cap = TS_ORDER_CAPABILITYSET()
			cap.orderFlags = ORDERFLAG.ZEROBOUNDSDELTASSUPPORT | ORDERFLAG.NEGOTIATEORDERSUPPORT #do not change this!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
			caps.append(cap)

			cap = TS_BITMAPCACHE_CAPABILITYSET()
			caps.append(cap)

			cap = TS_POINTER_CAPABILITYSET()
			caps.append(cap)

			cap = TS_INPUT_CAPABILITYSET()
			cap.inputFlags = INPUT_FLAG.SCANCODES
			cap.keyboardLayout = self.iosettings.keyboard_layout
			cap.keyboardType = self.iosettings.keyboard_type
			cap.keyboardSubType = self.iosettings.keyboard_subtype
			cap.keyboardFunctionKey = self.iosettings.keyboard_functionkey
			caps.append(cap)

			cap = TS_BRUSH_CAPABILITYSET()
			caps.append(cap)

			cap = TS_GLYPHCACHE_CAPABILITYSET()
			caps.append(cap)

			cap = TS_OFFSCREEN_CAPABILITYSET()
			caps.append(cap)

			cap = TS_VIRTUALCHANNEL_CAPABILITYSET()
			cap.flags = VCCAPS.COMPR_CS_8K | VCCAPS.COMPR_SC
			caps.append(cap)

			cap = TS_SOUND_CAPABILITYSET()
			caps.append(cap)

			share_hdr = TS_SHARECONTROLHEADER()
			share_hdr.pduType = PDUTYPE.CONFIRMACTIVEPDU
			share_hdr.pduVersion = 1
			share_hdr.pduSource = self.__joined_channels['MCS'].channel_id

			msg = TS_CONFIRM_ACTIVE_PDU()
			msg.shareID = 0x103EA
			msg.originatorID = 1002
			for cap in caps:
				msg.capabilitySets.append(TS_CAPS_SET.from_capability(cap))
			
			sec_hdr = None
			if self.cryptolayer is not None:
				sec_hdr = TS_SECURITY_HEADER()
				sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
				sec_hdr.flagsHi = 0

			await self.__joined_channels['MCS'].data_in_queue.put((msg, sec_hdr, None, share_hdr))

			data, err = await self.__joined_channels['MCS'].out_queue.get()
			if err is not None:
				raise err
			
			shc = TS_SHARECONTROLHEADER.from_bytes(data)
			if shc.pduType == PDUTYPE.DATAPDU:
				shd = TS_SHAREDATAHEADER.from_bytes(data)
				if shd.pduType2 == PDUTYPE2.SET_ERROR_INFO_PDU:
					# we got an error!
					res = TS_SET_ERROR_INFO_PDU.from_bytes(data)
					raise Exception('Server replied with error! Code: %s ErrName: %s' % (hex(res.errorInfoRaw), res.errorInfo.name))

				elif shd.pduType2 == PDUTYPE2.SYNCHRONIZE:
					# this is the expected data here
					res = TS_SYNCHRONIZE_PDU.from_bytes(data)
			
				else:
					raise Exception('Unexpected reply! %s' % shd.pduType2.name)
			else:
				raise Exception('Unexpected reply! %s' % shc.pduType.name)

			data_hdr = TS_SHAREDATAHEADER()
			data_hdr.shareID = 0x103EA
			data_hdr.streamID = STREAM_TYPE.MED
			data_hdr.pduType2 = PDUTYPE2.SYNCHRONIZE

			cli_sync = TS_SYNCHRONIZE_PDU()
			cli_sync.targetUser = self.__joined_channels['MCS'].channel_id
			sec_hdr = None
			if self.cryptolayer is not None:
				sec_hdr = TS_SECURITY_HEADER()
				sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
				sec_hdr.flagsHi = 0
			await self.__joined_channels['MCS'].data_in_queue.put((cli_sync, sec_hdr, data_hdr, None))

			data_hdr = TS_SHAREDATAHEADER()
			data_hdr.shareID = 0x103EA
			data_hdr.streamID = STREAM_TYPE.MED
			data_hdr.pduType2 = PDUTYPE2.CONTROL

			cli_ctrl = TS_CONTROL_PDU()
			cli_ctrl.action = CTRLACTION.COOPERATE
			cli_ctrl.grantId = 0
			cli_ctrl.controlId = 0

			sec_hdr = None
			if self.cryptolayer is not None:
				sec_hdr = TS_SECURITY_HEADER()
				sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
				sec_hdr.flagsHi = 0

			await self.__joined_channels['MCS'].data_in_queue.put((cli_ctrl, sec_hdr, data_hdr, None))
			

			data_hdr = TS_SHAREDATAHEADER()
			data_hdr.shareID = 0x103EA
			data_hdr.streamID = STREAM_TYPE.MED
			data_hdr.pduType2 = PDUTYPE2.CONTROL

			cli_ctrl = TS_CONTROL_PDU()
			cli_ctrl.action = CTRLACTION.REQUEST_CONTROL
			cli_ctrl.grantId = 0
			cli_ctrl.controlId = 0

			sec_hdr = None
			if self.cryptolayer is not None:
				sec_hdr = TS_SECURITY_HEADER()
				sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
				sec_hdr.flagsHi = 0

			await self.__joined_channels['MCS'].data_in_queue.put((cli_ctrl, sec_hdr, data_hdr, None))

			data_hdr = TS_SHAREDATAHEADER()
			data_hdr.shareID = 0x103EA
			data_hdr.streamID = STREAM_TYPE.MED
			data_hdr.pduType2 = PDUTYPE2.FONTLIST

			cli_font = TS_FONT_LIST_PDU()
			
			sec_hdr = None
			if self.cryptolayer is not None:
				sec_hdr = TS_SECURITY_HEADER()
				sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
				sec_hdr.flagsHi = 0

			await self.__joined_channels['MCS'].data_in_queue.put((cli_font, sec_hdr, data_hdr, None))
			
			return True, None
		except Exception as e:
			return None, e

	async def __x224_reader(self):
		# recieves X224 packets and dispatches each packet to the appropriate channel
		# gets activated when all channel setup is done
		# dont activate it before this!!!!
		
		try:
			while True:
				response, err = await self._x224net.in_queue.get()
				if err is not None:
					raise err

				if response is None:
					raise Exception('Server terminated the connection!')
				#print('__x224_reader data in -> %s' % response.data)
				x = self._t125_per_codec.decode('DomainMCSPDU', response.data)
				#print('__x224_reader decoded data in -> %s' % str(x))
				if x[0] != 'sendDataIndication':
					#print('Unknown packet!')
					continue
				
				data = x[1]['userData']
				if data is not None:
					if self.cryptolayer is not None:
						sec_hdr = TS_SECURITY_HEADER1.from_bytes(data)
						if SEC_HDR_FLAG.ENCRYPT in sec_hdr.flags:
							orig_data = data[12:]
							data = self.cryptolayer.client_dec(orig_data)
							if SEC_HDR_FLAG.SECURE_CHECKSUM in sec_hdr.flags:
								mac = self.cryptolayer.calc_salted_mac(data, is_server=True)
							else:
								mac = self.cryptolayer.calc_mac(data)
							if mac != sec_hdr.dataSignature:
								print('ERROR! Signature mismatch! Printing debug data')
								print('Encrypted data: %s' % orig_data)
								print('Decrypted data: %s' % data)
								print('Original MAC  : %s' % sec_hdr.dataSignature)
								print('Calculated MAC: %s' % mac)
				
				await self.__channel_id_lookup[x[1]['channelId']].raw_in_queue.put((data, None))

		except Exception as e:
			traceback.print_exc()
			return None, e

	async def __fastpath_reader(self):
		# Fastpath was introduced to the RDP specs to speed up data transmission
		# by reducing 4 useless layers from the traffic.
		# Transmission on this channel starts immediately after connection sequence
		# mostly video and pointer related info coming in from the server.
		# interesting note: it seems newer servers (>=win2016) only support this protocol of sending
		# high bandwith traffic. If you disable fastpath (during connection sequence) you won't
		# get images at all
		try:
			while True:
				response, err = await self.__tpkgnet.fastpath_in_queue.get()
				if err is not None:
					raise err
				if response is None:
					raise Exception('Server terminated the connection!')

				try:
					#print('fastpath data in -> %s' % len(response))
					fpdu = TS_FP_UPDATE_PDU.from_bytes(response)
					if FASTPATH_SEC.ENCRYPTED in fpdu.flags:
						data = self.cryptolayer.client_dec(fpdu.fpOutputUpdates)
						if FASTPATH_SEC.SECURE_CHECKSUM in fpdu.flags:
							mac = self.cryptolayer.calc_salted_mac(data, is_server=True)
						else:
							mac = self.cryptolayer.calc_mac(data)
						if mac != fpdu.dataSignature:
							print('ERROR! Signature mismatch! Printing debug data')
							print('FASTPATH_SEC  : %s' % fpdu)
							print('Encrypted data: %s' % fpdu.fpOutputUpdates[:100])
							print('Decrypted data: %s' % data[:100])
							print('Original MAC  : %s' % fpdu.dataSignature)
							print('Calculated MAC: %s' % mac)
							raise Exception('Signature mismatch')
						fpdu.fpOutputUpdates = TS_FP_UPDATE.from_bytes(data)

					if fpdu.fpOutputUpdates.fragmentation != FASTPATH_FRAGMENT.SINGLE:
						print('WARNING! FRAGMENTATION IS NOT IMPLEMENTED! %s' % fpdu.fpOutputUpdates.fragmentation)
					if fpdu.fpOutputUpdates.updateCode == FASTPATH_UPDATETYPE.BITMAP:
						#print('bitmap')
						for bitmapdata in fpdu.fpOutputUpdates.update.rectangles:
							await self.ext_out_queue.put(RDP_VIDEO.from_bitmapdata(bitmapdata))
					#else:
					#	if fpdu.fpOutputUpdates.updateCode not in [FASTPATH_UPDATETYPE.CACHED, FASTPATH_UPDATETYPE.POINTER]:
					#		print('notbitmap %s' % fpdu.fpOutputUpdates.updateCode.name)
				except Exception as e:
					# the decoder is not perfect yet, so it's better to keep this here...
					traceback.print_exc()
					return
				
				
		except Exception as e:
			traceback.print_exc()
			return None, e
	
	async def __external_reader(self):
		# This coroutine handles keyboard/mouse etc input from the user
		# It wraps the data in it's appropriate format then dispatches it to the server
		try:
			while True:
				indata = await self.ext_in_queue.get()
				if indata is None:
					#signaling exit
					await self.terminate()
					return
				if indata.type == RDPDATATYPE.KEYSCAN:
					indata = cast(RDP_KEYBOARD_SCANCODE, indata)
					data_hdr = TS_SHAREDATAHEADER()
					data_hdr.shareID = 0x103EA
					data_hdr.streamID = STREAM_TYPE.MED
					data_hdr.pduType2 = PDUTYPE2.INPUT
					
					kbi = TS_KEYBOARD_EVENT()
					kbi.keyCode = indata.keyCode
					kbi.keyboardFlags = 0
					if indata.is_pressed is False:
						kbi.keyboardFlags |= KBDFLAGS.RELEASE
					if indata.is_extended is True:
						kbi.keyboardFlags |= KBDFLAGS.EXTENDED
					clii_kb = TS_INPUT_EVENT.from_input(kbi)
					cli_input = TS_INPUT_PDU_DATA()
					cli_input.slowPathInputEvents.append(clii_kb)

					sec_hdr = None
					if self.cryptolayer is not None:
						sec_hdr = TS_SECURITY_HEADER()
						sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
						sec_hdr.flagsHi = 0

					await self.__joined_channels['MCS'].data_in_queue.put((cli_input, sec_hdr, data_hdr, None))
			
				elif indata.type == RDPDATATYPE.MOUSE:
					if indata.xPos < 0 or indata.yPos < 0:
						continue
					indata = cast(RDP_MOUSE, indata)
					data_hdr = TS_SHAREDATAHEADER()
					data_hdr.shareID = 0x103EA
					data_hdr.streamID = STREAM_TYPE.MED
					data_hdr.pduType2 = PDUTYPE2.INPUT
					
					mouse = TS_POINTER_EVENT()
					mouse.pointerFlags = 0
					if indata.pressed is True:
						mouse.pointerFlags |= PTRFLAGS.DOWN
					if indata.button == 1:
						mouse.pointerFlags |= PTRFLAGS.BUTTON1
					if indata.button == 2:
						mouse.pointerFlags |= PTRFLAGS.BUTTON2
					if indata.button == 3:
						mouse.pointerFlags |= PTRFLAGS.BUTTON3
					if indata.button == 0:
						# indicates a simple pointer update with no buttons pressed
						# sending this enables the mouse hover feel on the remote end
						mouse.pointerFlags |= PTRFLAGS.MOVE
					mouse.xPos = indata.xPos
					mouse.yPos = indata.yPos

					clii_mouse = TS_INPUT_EVENT.from_input(mouse)
					
					cli_input = TS_INPUT_PDU_DATA()
					cli_input.slowPathInputEvents.append(clii_mouse)

					sec_hdr = None
					if self.cryptolayer is not None:
						sec_hdr = TS_SECURITY_HEADER()
						sec_hdr.flags = SEC_HDR_FLAG.ENCRYPT
						sec_hdr.flagsHi = 0

					
					await self.__joined_channels['MCS'].data_in_queue.put((cli_input, sec_hdr, data_hdr, None))

				elif indata.type == RDPDATATYPE.CLIPBOARD_DATA_TXT:
					if 'cliprdr' not in self.__joined_channels:
						print('Got clipboard data but no clipboard channel setup!')
						continue
					await self.__joined_channels['cliprdr'].in_queue.put(indata)

		except Exception as e:
			traceback.print_exc()
			return None, e
	
async def amain():
	try:
		from aardwolf.commons.url import RDPConnectionURL
		from aardwolf.commons.iosettings import RDPIOSettings

		iosettings = RDPIOSettings()
		url = 'rdp+ntlm-password://TEST\\Administrator:Passw0rd!1@10.10.10.103'
		rdpurl = RDPConnectionURL(url)
		conn = rdpurl.get_connection(iosettings)
		_, err = await conn.connect()
		if err is not None:
			raise err
		
		while True:
			data = await conn.ext_out_queue.get()
			print(data)
	except Exception as e:
		traceback.print_exc()

	

if __name__ == '__main__':
	asyncio.run(amain())