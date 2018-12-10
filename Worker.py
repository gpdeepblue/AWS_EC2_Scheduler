#!/usr/bin/python
import boto3
import time
from botocore.exceptions import ClientError
from distutils.util import strtobool
from SSMDelegate import SSMDelegate
import botocore
import Utils
import logging
from redo import retriable, retry #https://github.com/mozilla-releng/redo



__author__ = "Gary Silverman"

logger = logging.getLogger('Orchestrator')  # The Module Name


class Worker(object):
	SNS_SUBJECT_PREFIX_WARNING = "Warning:"
	SNS_SUBJECT_PREFIX_INFORMATIONAL = "Info:"


	def __init__(self, workloadRegion, instance, ec2_client, dryRunFlag, snsInit):
		self.workloadRegion = workloadRegion
		self.instance = instance
		self.dryRunFlag = dryRunFlag
		self.ec2_client = ec2_client
		self.snsInit = snsInit
		self.instanceStateMap = {
			"pending": 0,
			"running": 16,
			"shutting-down": 32,
			"terminated": 48,
			"stopping": 64,
			"stopped": 80
		}

		try:
			self.ec2Resource = boto3.resource('ec2', region_name=self.workloadRegion)
		except Exception as e:
			msg = 'Worker::__init__() Exception obtaining botot3 ec2 resource in region %s -->' % workloadRegion
			logger.error(msg + str(e))


class StartWorker(Worker):

	def __init__(self, ddbRegion, workloadRegion, instance, all_elbs, elb, scalingInstanceDelay, dryRunFlag, ec2_client, snsInit):
		super(StartWorker, self).__init__(workloadRegion, instance, ec2_client, dryRunFlag, snsInit)

		self.ddbRegion = ddbRegion
		self.all_elbs = all_elbs
		self.elb = elb
		self.scalingInstanceDelay = scalingInstanceDelay

	@retriable(attempts=5, sleeptime=0, jitter=0)
	def addressELBRegistration(self):
		for i in self.all_elbs['LoadBalancerDescriptions']:
			for j in i['Instances']:
				if j['InstanceId'] == self.instance.id:
					elb_name = i['LoadBalancerName']
					logger.info("Instance %s is attached to ELB %s, and will be deregistered and re-registered" % (self.instance.id, elb_name))
					success_deregister_done = 0
					while (success_deregister_done == 0):
						try:
							self.elb.deregister_instances_from_load_balancer(LoadBalancerName=elb_name, Instances=[{'InstanceId': self.instance.id}])
							logger.debug("Succesfully deregistered instance %s from load balancer %s" % (self.instance.id, elb_name))
							success_deregister_done=1
						except Exception as e:
							logger.warning('Worker::addressELBRegistration()::deregister_instances_from_load_balancer() encountered an exception of -->' + str(e))
							raise e

					success_register_done=0
					while (success_register_done == 0):
						try:
							self.elb.register_instances_with_load_balancer(LoadBalancerName=elb_name,Instances=[{'InstanceId': self.instance.id}])
							logger.debug('Succesfully registered instance %s to load balancer %s' % (self.instance.id, elb_name))
							success_register_done=1
						except Exception as e:
							logger.warning('Worker::addressELBRegistration()::register_instances_with_load_balancer() encountered an exception of -->' + str(e))
							raise e

	def startInstance(self):

			result = 'Instance not started'
			if (self.dryRunFlag):
				logger.warning('DryRun Flag is set - instance will not be started')
			else:
				if self.all_elbs != "0":
					logger.debug('addressELBRegistration() for %s' % self.instance.id)
					try:
						self.addressELBRegistration()
					except Exception as e:
						self.snsInit.sendSns("Worker::addressELBRegistration() has encountered an exception", str(e))

					logger.debug('Starting EC2 instance: %s' % self.instance.id)
					try:
						result = retry(self.instance.start, attempts = 5, sleeptime=0, jitter=0)
						logger.debug('Starting EC2 instance: %s' % self.instance.id)
						logger.info('startInstance() for ' + self.instance.id + ' result is %s' % result)
					except Exception as e:
						msg = 'Worker::instance.start() Exception encountered during instance start ---> %s' % e
						logger.error(msg)
						self.snsInit.sendSns("Exception - EC2 instance start::Worker::instance.start() Exception encountered during instance start",str(e))


	def scaleInstance(self, modifiedInstanceType):

		instanceState = self.instance.state
		if (instanceState['Name'] == 'stopped'):

			result = 'no result'
			if (self.dryRunFlag):
				logger.warning('DryRun Flag is set - instance will not be scaled')

			else:
				logger.info(
					'Instance [%s] will be scaled to Instance Type [%s]' % (self.instance.id, modifiedInstanceType))

				# Extract whole instance type from DDB into a list
				modifiedInstanceTypeList = modifiedInstanceType.split('.')
				targetInstanceFamily = modifiedInstanceTypeList[0]

				# EC2.Instance.modify_attribute()
				# Check and exclude non-optimized instance families. Should be enhanced to be a map.  Currently added as hotfix.
				preventEbsOptimizedList = ['t2']
				if (targetInstanceFamily in preventEbsOptimizedList):
					ebsOptimizedAttr = False
					# Check T2 Unlimited
					self.t2Unlimited(modifiedInstanceTypeList)
				else:
					ebsOptimizedAttr = self.instance.ebs_optimized  # May have been set to True or False previously

				modifiedInstanceTypeValue = modifiedInstanceTypeList[0] + '.' + modifiedInstanceTypeList[1]
				InstanceTypeKwargs = {"InstanceType":{'Value': modifiedInstanceTypeValue}}
				try:
					result = retry(self.instance.modify_attribute,attempts=5,jitter=0,sleeptime=0,kwargs=InstanceTypeKwargs)
				except Exception as e:
					msg = 'Worker::instance.modify_attribute().modifiedInstanceTypeValue Exception for EC2 instance %s , error --> %s ' % (self.instance, str(e))
					logger.warning(msg)
					self.snsInit.sendSns("Worker::instance.modify_attribute().modifiedInstanceTypeValue has encountered an exception",str(e))

				try:
					result = retry(self.compareInstanceTypeValues, attempts = 5, sleeptime=3, jitter=0,args=(modifiedInstanceType,))
				except Exception as e:
					msg = 'Worker::compareInstanceTypeValues() Exception for instance %s , error --> currentInstanceTypeValue does not match modifiedInstanceTypeValue' % (self.instance)
					logger.warning(msg)
					self.snsInit.sendSns("Worker::compareInstanceTypeValues() has encountered an exception",str(e))
										
				EbsOptimizeKwargs = {"EbsOptimized":{'Value': ebsOptimizedAttr}}
				try:
					result = retry(self.instance.modify_attribute,attempts=5,sleeptime=0,jitter=0,kwargs=EbsOptimizeKwargs)
				except Exception as e:
					msg = 'Worker::instance.modify_attribute().ebsOptimizedAttr Exception for EC2 instance %s, error --> %s' % (self.instance, str(e))
					logger.warning(msg)
					self.snsInit.sendSns("Worker::instance.modify_attribute().modifiedInstanceTypeValue has encountered an exception",str(e))

				# It appears the start instance reads 'modify_attribute' changes as eventually consistent in AWS (assume DynamoDB),
				#    this can cause an issue on instance type change, whereby the LaunchPlan generates an exception.
				#    To mitigate against this, we will introduce a one second sleep delay after modifying an attribute
				time.sleep(self.scalingInstanceDelay)

				logger.info('scaleInstance() for ' + self.instance.id + ' result is %s' % result)
		else:
			logMsg = 'scaleInstance() requested to change instance type for non-stopped instance ' + self.instance.id + ' no action taken'
			logger.warning(logMsg)


	def compareInstanceTypeValues(self, modifiedInstanceTypeValue):
		currentInstanceTypeValue = list(self.ec2_client.describe_instance_attribute(InstanceId=self.instance.id, Attribute='instanceType')['InstanceType'].values())
		logger.info('compareInstanceTypeValues() - Comparing Instance Type Values of %s' % (self.instance.id))
		logger.info('compareInstanceTypeValues() -   modifiedInstanceTypeValue : %s ' % (modifiedInstanceTypeValue))
		logger.info('compareInstanceTypeValues() -    currentInstanceTypeValue : %s ' % (currentInstanceTypeValue))
		if(currentInstanceTypeValue[0] not in modifiedInstanceTypeValue):
			raise ValueError("Current instance type value does not match modified instance type value.")
		

	def t2Unlimited(self, modifiedInstanceList):

		if len(modifiedInstanceList) == 3:
			self.modifiedInstanceFlag = modifiedInstanceList[2]
			logger.info('t2Unlimited(): Checking if T2 Unlimited flag is specified in DynamoDB')
			logger.debug('t2Unlimited(): t2_unlimited_flag: %s' % self.modifiedInstanceFlag)
			if (self.modifiedInstanceFlag == "u") or (self.modifiedInstanceFlag == "U"):
				logger.debug('t2Unlimited(): Found T2 Flag in DynamoDB: %s' % self.modifiedInstanceFlag)
				try:
					self.checkT2Unlimited()
				except Exception as e:
					self.snsInit.sendSns("Worker::describe_instance_credit_specifications() has encountered an exception" ,str(e))


				if self.current_t2_value == "standard":
					try:
						logger.info('t2Unlimited(): Trying to modify EC2 instance credit specification')
						result = retry(self.ec2_client.modify_instance_credit_specification,attempts=5, sleeptime=0,jitter=0,kwargs= {"InstanceCreditSpecifications":[{'InstanceId': self.instance.id,'CpuCredits': 'unlimited'}]} )
						logger.info('t2Unlimited(): EC2 instance credit specification modified')
					except Exception as e:
						msg = 'Worker::instance.modify_attribute().t2Unlimited Exception for EC2 instance %s, error --> %s' % (self.instance, str(e))
						logger.warning(msg)
						self.snsInit.sendSns("Worker::checkT2Unlimited::instance.modify_attribute().t2Unlimited has encountered an exception",str(e))

		else:
			try:
				self.checkT2Unlimited()
			except Exception as e:
				self.snsInit.sendSns("Worker::checkT2Unlimited::describe_instance_credit_specifications() Exception has encountered an exception",str(e))

			if self.current_t2_value == "unlimited":
				logger.debug('t2Unlimited(): Current T2 value: %s' % self.current_t2_value)

				try:
					logger.info('t2Unlimited(): Trying to modify EC2 instance credit specification')
					result = retry(self.ec2_client.modify_instance_credit_specification, attempts=5, sleeptime=0, jitter=0,
					kwargs= {"InstanceCreditSpecifications":[{'InstanceId': self.instance.id,'CpuCredits': 'standard'}]} )
					logger.info('t2Unlimited(): EC2 instance credit specification modified')

				except Exception as e:
					msg = 'Worker::instance.modify_attribute().t2standard Exception in progress for EC2 instance %s, error --> %s' % (self.instance, str(e))
					logger.warning(msg)
					self.snsInit.sendSns("Worker::instance.modify_attribute().t2standard Exception has encountered an exception",str(e))

	@retriable(attempts=5,sleeptime=0, jitter=0)
	def checkT2Unlimited(self):
		try:
			logger.info('check_t2_unlimited(): Checking current T2 value')
			self.current_t2_value = self.ec2_client.describe_instance_credit_specifications(InstanceIds=[self.instance.id,])['InstanceCreditSpecifications'][0]['CpuCredits']

		except Exception as e:
			msg = 'Worker::describe_instance_credit_specifications() exception for EC2 instance %s, error --> %s' % (self.instance, str(e))
			logger.warning(msg)
			raise e

		return self.current_t2_value


	def start(self):
		self.startInstance()



class StopWorker(Worker):

	def __init__(self, ddbRegion, workloadRegion, instance, dryRunFlag, ec2_client, snsInit):
		super(StopWorker, self).__init__(workloadRegion, instance, ec2_client, dryRunFlag, snsInit)
		self.ddbRegion = ddbRegion

		# MUST convert string False to boolean False
		self.waitFlag = strtobool('False')
		self.overrideFlag = strtobool('False')
		self.snsInit = snsInit

	def stopInstance(self):

		logger.debug('Worker::stopInstance() called')

		result = 'Instance not Stopped'

		if (self.dryRunFlag):
			logger.warning('DryRun Flag is set - instance will not be stopped')
		else:
			try:
				result = retry(self.instance.stop, attempts=5, sleeptime=0, jitter=0)
				logger.debug("Succesfully stopped EC2 instance %s" % (self.instance.id))
				logger.info('stopInstance() for ' + self.instance.id + ' result is %s' % result)
			except Exception as e:
				msg = 'Worker::instance.stop() Exception occured for EC2 instance %s, error --> %s' % (self.instance,  str(e))
				logger.warning(msg)
				self.snsInit.sendSns("Exception instance.stopInstance() Exception encountered during instance stop",str(e))


		# If configured, wait for the stop to complete prior to returning
		logger.debug('The bool value of self.waitFlag %s, is %s' % (self.waitFlag, bool(self.waitFlag)))

		# self.waitFlag has been converted from str to boolean via set method
		if (self.waitFlag):
			logger.info(self.instance.id + ' :Waiting for Stop to complete...')

			if (self.dryRunFlag):
				logger.warning('DryRun Flag is set - waiter() will not be employed')
			else:
				try:
					# Need the Client to get the Waiter
					ec2Client = self.ec2Resource.meta.client
					waiter = ec2Client.get_waiter('instance_stopped')

					# Waits for 40 15 second increments (e.g. up to 10 minutes)
					waiter.wait()

				except Exception as e:
					logger.warning('Worker:: waiter block encountered an exception of -->' + str(e))

		else:
			logger.info(self.instance.id + ' Wait for Stop to complete was not requested')

	def setWaitFlag(self, flag):

		# MUST convert string False to boolean False
		self.waitFlag = strtobool(flag)

	def getWaitFlag(self):
		return (self.waitFlag)

	def isOverrideFlagSet(self, S3BucketName, S3KeyPrefixName, overrideFileName, osType):
		''' Use SSM to check for existence of the override file in the guest OS.  If exists, don't Stop instance but log.
		Returning 'True' means the instance will not be stopped.
			Either because the override file exists, or the instance couldn't be reached
		Returning 'False' means the instance will be stopped (e.g. not overridden)
		'''

		# If there is no overrideFilename specified, we need to return False.  This is required because the
		# remote evaluation script may evaluate to "Bypass" with a null string for the override file.  Keep in
		# mind, DynamodDB isn't going to enforce an override filename be set in the directive.

		noSSM = ""

		if (S3BucketName == noSSM) or (S3KeyPrefixName == noSSM) or (not overrideFileName):
			logger.info(
				self.instance.id + ' Override Flag not set in specification.  Therefore this instance will be actioned. ')
			return False

		if not osType:
			logger.info(
				self.instance.id + ' Override Flag set BUT no Operating System attribute in specification. Therefore this instance will be actioned.')
			return False

		# Create the delegate
		ssmDelegate = SSMDelegate(self.instance.id, S3BucketName, S3KeyPrefixName, overrideFileName, osType,
								  self.ddbRegion, logger, self.workloadRegion)

		# Very first thing to check is whether SSM is going to write the output to the S3 bucket.
		#   If the bucket is not in the same region as where the instances are running, then SSM doesn't write
		#   the result to the bucket and thus the rest of this method is completely meaningless to run.

		if (ssmDelegate.isS3BucketInWorkloadRegion() == SSMDelegate.S3_BUCKET_IN_CORRECT_REGION):

			warningMsg = ''
			msg = ''
			# Send request via SSM, and check if send was successful
			ssmSendResult = ssmDelegate.sendSSMCommand()
			if (ssmSendResult):
				# Have delegate advise if override file was set on instance.  If so, the instance is not to be stopped.
				overrideRes = ssmDelegate.retrieveSSMResults(ssmSendResult)
				logger.debug('SSMDelegate retrieveSSMResults() results :' + overrideRes)

				if (overrideRes == SSMDelegate.S3_BUCKET_IN_WRONG_REGION):
					# Per SSM, the bucket must be in the same region as the target instance, otherwise the results will not be writte to S3 and cannot be obtained.
					self.overrideFlag = True
					warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING + ' ' + self.instance.id + ' Instance will be not be stopped because the S3 bucket is not in the same region as the workload'
					logger.warning(warningMsg)

				elif (overrideRes == SSMDelegate.DECISION_STOP_INSTANCE):
					# There is a result and it specifies it is ok to Stop
					self.overrideFlag = False
					logger.info(self.instance.id + ' Instance will be stopped')

				elif (overrideRes == SSMDelegate.DECISION_NO_ACTION_UNEXPECTED_RESULT):
					# Unexpected SSM Result, see log file.  Will default to overrideFlag==true out of abundance for caution
					self.overrideFlag = True
					warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING + ' ' + self.instance.id + ' Instance will be not be stopped as there was an unexpected SSM result.'
					logger.warning(warningMsg)

				elif (overrideRes == SSMDelegate.DECISION_RETRIES_EXCEEDED):
					self.overrideFlag = True
					warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING + ' ' + self.instance.id + ' Instance will be not be stopped # retries to collect SSM result from S3 was exceeded'
					logger.warning(warningMsg)

				else:
					# Every other result means the instance will be bypassed (e.g. not stopped)
					self.overrideFlag = True
					msg = Worker.SNS_SUBJECT_PREFIX_INFORMATIONAL + ' ' + self.instance.id + ' Instance will be not be stopped because override file was set'
					logger.info(msg)

			else:
				self.overrideFlag = True
				warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING + ' ' + self.instance.id + ' Instance will be not be stopped because SSM could not query it'
				logger.warning(warningMsg)

		else:
			self.overrideFlag = True
			warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING + ' SSM will not be executed as S3 bucket is not in the same region as the workload. [' + self.instance.id + '] Instance will be not be stopped'
			logger.warning(warningMsg)

		if (self.overrideFlag == True):
			if (warningMsg):
				self.snsInit.sendSns(Worker.SNS_SUBJECT_PREFIX_WARNING, warningMsg)
			else:
				self.snsInit.sendSns(Worker.SNS_SUBJECT_PREFIX_INFORMATIONAL, warningMsg)
		return (self.overrideFlag)

	def setOverrideFlagSet(self, overrideFlag):
		self.overrideFlag = strtobool(overrideFlag)

	def execute(self, S3BucketName, S3KeyPrefixName, overrideFileName, osType):
		if (self.isOverrideFlagSet(S3BucketName, S3KeyPrefixName, overrideFileName, osType) == False):
			self.stopInstance()
