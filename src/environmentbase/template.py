from troposphere import Output, Ref, Join, Parameter, Base64, GetAtt, FindInMap, Retain, Select
from troposphere import iam, ec2, autoscaling, route53 as r53, s3, logs
from awacs import logs as awacs_logs, aws
from awacs.helpers.trust import make_simple_assume_statement
import troposphere as t
import troposphere.constants as tpc
import troposphere.elasticloadbalancing as elb
import troposphere.cloudformation as cf
import hashlib
import json
import os
import time
from datetime import datetime
import resources as res
import utility

class Template(t.Template):
    """
    Custom wrapper for Troposphere Template object which handles S3 uploads and a specific
    workflow around hashing the template to allow for a validation mechanism of a template's
    consistency since it was generated.
    """

    """
    Class variable for S3 destination, set once by controller for use across all template w/in an environment
    """
    s3_path_prefix = ''

    """
    S3 bucket name
    """
    template_bucket = ''

    """
    Child stack timeout
    """
    stack_timeout = '60'

    def __init__(self, template_name, root_template=False):
        """
        Init method for environmentbase.Template class
        @param template_name [string] - name of this template, used when identifying this template when uploading, etc.
        """
        t.Template.__init__(self)
        self.name = template_name
        self.AWSTemplateFormatVersion = ''

        self._vpc_cidr = None
        self._vpc_id = None
        self._common_security_group = None
        self._utility_bucket = None
        self._igw = None
        self._child_templates = []
        self.manual_parameter_bindings = {}
        self._resource_path = ''

        self._azs = []
        self._subnets = {}

        self._is_root_template = root_template

    def _ref_maybe(self, item):
        """
        Wraps provided item in a troposphere.Ref() if the type makes sense to ref in cloudformation.
        This allows attributes to be saved w/o needing to ref() them before (or after).
        Note: Dicts and Lists are recursively processed for 'ref'able values
        """
        # Wrap input if type is:
        # - AWSDeclaration --> Parameters & Outputs
        # - AWSObject --> Resources
        # - items in a list or valued of a hash
        if isinstance(item, (t.AWSDeclaration, t.AWSObject)):
            return Ref(item)

        elif isinstance(item, list):
            items = []
            for i in item:
                items.append(self._ref_maybe(i))
            return items

        elif isinstance(item, dict):
            items = {}
            for (k, v) in item.iteritems():
                items.update({k: self._ref_maybe(v)})
            return items

        else:
            return item

    @property
    def vpc_cidr(self):
        return self._ref_maybe(self._vpc_cidr)

    @property
    def vpc_id(self):
        return self._ref_maybe(self._vpc_id)

    @property
    def common_security_group(self):
        return self._ref_maybe(self._common_security_group)

    @property
    def utility_bucket(self):
        return self._ref_maybe(self._utility_bucket)

    @property
    def igw(self):
        return self._ref_maybe(self._igw)

    @property
    def ec2_key(self):
        return self._ref_maybe(self._ec2_key)

    @property
    def vpc_gateway_attachment(self):
        return self._ref_maybe(self._vpc_gateway_attachment)

    @property
    def azs(self):
        return self._ref_maybe(self._azs)

    @property
    def subnets(self):
        return self._ref_maybe(self._subnets)

    @property
    def resource_path(self):
        if not self._resource_path:
            include_timestamp = not self._is_root_template
            self._resource_path = utility.template_s3_resource_path(
                Template.s3_path_prefix,
                self.name,
                include_timestamp=include_timestamp)
        return self._resource_path

    def __get_template_hash(self):
        """
        Private method holds process for hashing this template for future validation.
        """
        m = hashlib.sha256()
        m.update(self.__validation_formatter())
        return m.hexdigest()

    def merge(self, other_template):
        """
        Experimental merge function
        1. This passes all the initialized attributes to the other template
        2. Calls the other template's build_hook()
        3. Copies the generated troposphere attributes back into this template
        """
        other_template.copy_attributes_from(self)

        other_template.build_hook()

        self.metadata.update(other_template.metadata)
        self.conditions.update(other_template.conditions)
        self.mappings.update(other_template.mappings)
        self.outputs.update(other_template.outputs)
        self.parameters.update(other_template.parameters)
        self.resources.update(other_template.resources)

    def copy_attributes_from(self, other_template):
        """
        Copies all attributes from the other template into this one
        These typically get initialized for a template when add_child_template is called
        from the controller, but that never happens when merging two templates
        """
        self._vpc_cidr               = other_template._vpc_cidr
        self._vpc_id                 = other_template._vpc_id
        self._common_security_group  = other_template._common_security_group
        self._utility_bucket         = other_template._utility_bucket
        self._igw                    = other_template._igw
        self._vpc_gateway_attachment = other_template._vpc_gateway_attachment

        self._azs        = list(other_template.azs)
        self._subnets    = other_template.subnets.copy()

        self.parameters = other_template.parameters.copy()
        self.mappings   = other_template.mappings.copy()
        self.metadata   = other_template.metadata.copy()
        self.conditions = other_template.conditions.copy()
        self.outputs    = other_template.outputs.copy()
        self.resources  = other_template.resources.copy()

    def build_hook(self):
        """
        Provides template subclasses a place to assemble resources with access to common parameters and mappings.
        Executed by add_child_template() after add_common_params_to_child_template() and load_ami_cache()
        """
        pass

    @staticmethod
    def get_config_schema():
        """
        This method is provided for subclasses to update config requirements with additional required keys and their types.
        The format is a dictionary with key values being one of bool/int/float/str/list.
        Example (yes comments are allowed):
        {
            "template": {
                // Name of json file containing mapping labels to AMI ids
                "ami_map_file": "basestring",
                "mock_upload": "bool",
            }
        }
        :return: dict of config settings to be merged into base config, match existing keys to replace.
        """
        return {}

    @staticmethod
    def get_factory_defaults():
        """
        This method is provided for subclasses to update factory default config file with additional sections.
        The format is basic json (with comment support).
        {
            "template": {
                // Name of json file containing mapping labels to AMI ids
                "ami_map_file": "ami_cache.json",
                "mock_upload": false,
            }
        }
        :return: dict of config settings to be merged into base config, match existing keys to replace.
        """
        return {}

    def to_template_json(self):
        """
        Centralized method for managing outputting this template with a timestamp identifying when it was generated and for creating a SHA256 hash representing the template for validation purposes
        """
        self.process_child_templates()

        # strip existing values
        for output_key in ['dateGenerated', 'templateValidationHash']:
            if output_key in self.outputs:
                self.outputs.pop(output_key)

        # set the date that this template was generated
        if 'dateGenerated' not in self.outputs:
            self.add_output(Output(
                'dateGenerated',
                Value=str(datetime.utcnow()),
                Description='UTC datetime representation of when this template was generated'))

        # generate the template validation hash
        if 'templateValidationHash' not in self.outputs:
            self.add_output(Output(
                'templateValidationHash',
                Value=self.__get_template_hash(),
                Description='Hash of this template that can be used as a simple means of validating whether a template has been changed since it was generated.'))

        return self.to_json()

    def validate_template(self):
        """
        Centralized method for validating this templates' templateValidationHash value
        """
        if 'templateValidationHash' not in self.outputs:
            raise ValueError('This template does not contain a templateValidationHash output value')
        else:
            output_value = self.outputs.pop('templateValidationHash')
            computed_hash = self.__get_template_hash()
            if output_value.Value != computed_hash:
                raise ValueError('Template failed validation check. Template hash is [' + output_value.get('Value') + '] and computed hash is [' + computed_hash + ']')
            else:
                return True

    def __validation_formatter(self):
        """
        Validation formatter helps to ensure consistent formatting for hash validation workflow
        """
        return json.dumps(json.loads(self.to_json()), separators=(',', ':'))

    def add_parameter_idempotent(self, troposphere_parameter):
        """
        Idempotent add (add only if not exists) for parameters within the template
        @param [Troposphere.Parameter] Troposphere Parameter to add to this template
        """
        if troposphere_parameter.title not in self.parameters:
            return self.add_parameter(troposphere_parameter)
        else:
            return None

    def add_instance_profile(self, layer_name, iam_policies, path_prefix):
        iam_role_obj = iam.Role(layer_name + 'IAMRole',
                AssumeRolePolicyDocument={
                    'Statement': [{
                        'Effect': 'Allow',
                        'Principal': {'Service': ['ec2.amazonaws.com']},
                        'Action': ['sts:AssumeRole']
                    }]},
                    Path=Join('', ['/' + path_prefix + '/', layer_name , '/']))

        if iam_policies != None:
            iam_role_obj.Policies = iam_policies

        iam_role = self.add_resource(iam_role_obj)

        return self.add_resource(iam.InstanceProfile(layer_name + 'InstancePolicy',
                Path='/' + path_prefix + '/',
                Roles=[Ref(iam_role)]))

    def add_common_parameters_from_parent(self, parent):
        ec2_key = parent._ec2_key.Default
        parent_subnets = parent._subnets
        az_count = len(parent._azs)
        region_map = parent.mappings['RegionMap']
        self.add_common_parameters(ec2_key, region_map, parent_subnets, az_count)

    def add_common_parameters(self, ec2_key, region_map, parent_subnets, az_count=2):
        """
        Adds parameters to template for use as a child stack:
            vpcCidr,
            vpcId,
            commonSecurityGroup,
            utilityBucket,
            each subnet: [public|private]Subnet[0-9],
            each AZ name: availabilityZone[0-9]
        """
        self._vpc_cidr = self.add_parameter(Parameter(
            'vpcCidr',
            Description='CIDR of the VPC network',
            Type='String',
            AllowedPattern=res.get_str('cidr_regex'),
            ConstraintDescription=res.get_str('cidr_regex_message')))

        self._vpc_id = self.add_parameter(Parameter(
            'vpcId',
            Description='ID of the VPC network',
            Type='String'))

        self._common_security_group = self.add_parameter(Parameter(
            'commonSecurityGroup',
            Description='Security Group ID of the common security group for this environment',
            Type='String'))

        self._utility_bucket = self.add_parameter(Parameter(
            'utilityBucket',
            Description='Name of the S3 bucket used for infrastructure utility',
            Type='String'))

        self._igw = self.add_parameter(Parameter(
            'internetGateway',
            Description='Name of the internet gateway used by the vpc',
            Type='String'))

        self._vpc_gateway_attachment = self.add_parameter(Parameter(
            'igwVpcAttachment',
            Description='VPCGatewayAttachment for the VPC and IGW',
            Type='String'))

        self._ec2_key = self.add_parameter(Parameter(
           'ec2Key',
            Type='String',
            Default=ec2_key,
            Description='Name of an existing EC2 KeyPair to enable SSH access to the instances',
            AllowedPattern=res.get_str('ec2_key'),
            MinLength=1,
            MaxLength=255,
            ConstraintDescription=res.get_str('ec2_key_message')
        ))

        self.mappings['RegionMap'] = region_map

        for subnet_type in parent_subnets:
            if subnet_type not in self._subnets:
                self._subnets[subnet_type] = {}

            for subnet_layer in parent_subnets[subnet_type]: 
                if subnet_layer not in self._subnets[subnet_type]:
                    self._subnets[subnet_type][subnet_layer] = []

                for subnet in parent_subnets[subnet_type][subnet_layer]:
                    self._subnets[subnet_type][subnet_layer].append(self.add_parameter(Parameter(
                        subnet.name,
                        Description=subnet.name,
                        Type='String')))

        self._azs = []

        for x in range(0, az_count):
            az_param = Parameter(
                'availabilityZone' + str(x),
                Description='Availability Zone ' + str(x),
                Type='String')
            self.add_parameter(az_param)
            self._azs.append(az_param)



    @staticmethod
    def build_bootstrap(bootstrap_files=None,
                        variable_declarations=None,
                        cleanup_commands=None,
                        prepend_line='#!/bin/bash'):
        """
        Method encapsulates process of building out the bootstrap given a set of variables and a bootstrap file to source from
        Returns base 64-wrapped, joined bootstrap to be applied to an instnace
        @param bootstrap_files [ string[] ] list of paths to the bash script(s) to read as the source for the bootstrap action to created
        @param variable_declaration [ list ] list of lines to add to the head of the file - used to inject bash variables into the script
        @param cleanup_commnds [ string[] ] list of lines to add at the end of the file - used for layer-specific details
        """
        if prepend_line != '':
            ret_val = [prepend_line]
        else:
            ret_val = []

        if variable_declarations is not None:
            for line in variable_declarations:
                ret_val.append(line)
        for file_name_or_content in bootstrap_files:
            for line in Template.get_file_contents(file_name_or_content):
                ret_val.append(line)
        if cleanup_commands is not None:
            for line in cleanup_commands:
                ret_val.append(line)
        return Base64(Join("\n", ret_val))

    @staticmethod
    def get_file_contents(file_name_or_content):
        """
        Method encpsulates reading a file into a list while removing newline characters.
        If file is not found the variable is interpreted as the file content itself.
        @param file_name_or_content [string] path to file to read or content itself
        """
        ret_val = []
        if not os.path.isfile(file_name_or_content):
            content = file_name_or_content.split('\n')
        else:
            with open(file_name_or_content) as f:
                content = f.readlines()

        for line in content:
            if not line.startswith('#~'):
                ret_val.append(line.replace("\n", ""))
        return ret_val

    def add_ami_mapping(self, json_data):
        """
        Method gets the ami cache from the file locally and adds a mapping for ami ids per region into the template
        This depends on populating ami_cache.json with the AMI ids that are output by the packer scripts per region
        @param ami_map_file [string] path representing where to find the AMI map to ingest into this template
        """
        for region in json_data:
            for key in json_data[region]:
                self.add_region_map_value(region, key, json_data[region][key])

    def add_region_map_value(self,
                             region,
                             key,
                             value):
        """
        Method adds a key value pair to the RegionMap mapping within this CloudFormation template
        @param region [string] AWS region name that the key value pair is associated with
        @param key [string] name of the key to store in the RegionMap mapping for the specified Region
        @param value [string] value portion of the key value pair related to the region specified
        """
        self.__init_region_map([region])
        if region not in self.mappings['RegionMap']:
            self.mappings['RegionMap'][region] = {}
        self.mappings['RegionMap'][region][key] = value

    def __init_region_map(self,
                          region_list):
        """
        Internal helper method used to check to ensure mapping dictionaries are present
        @param region_list [list(str)] array of strings representing the names of the regions to validate and/or create within the RegionMap CloudFormation mapping
        """
        if 'RegionMap' not in self.mappings:
            self.mappings['RegionMap'] = {}
        for region_name in region_list:
            if region_name not in self.mappings['RegionMap']:
                self.mappings['RegionMap'][region_name] = {}

    def add_asg(self,
                layer_name,
                instance_profile=None,
                instance_type='t2.micro',
                ami_name='amazonLinuxAmiId',
                ec2_key=None,
                user_data=None,
                security_groups=None,
                min_size=1,
                max_size=1,
                root_volume_size=None,
                root_volume_type=None,
                include_ephemerals=True,
                number_ephemeral_vols=2,
                ebs_data_volumes=None,  # [{'size':'100', 'type':'gp2', 'delete_on_termination': True, 'iops': 4000, 'volume_type': 'io1'}]
                custom_tags=None,
                load_balancer=None,
                instance_monitoring=False,
                subnet_layer=None,
                associate_public_ip=None,
                launch_config_metadata=None,
                creation_policy=None,
                update_policy=None,
                depends_on=[]):
        """
        Wrapper method used to create an EC2 Launch Configuration and Auto Scaling group
        @param layer_name [string] friendly name of the set of instances being created - will be set as the name for instances deployed
        @param instance_profile [Troposphere.iam.InstanceProfile] IAM Instance Profile object to be applied to instances launched within this Auto Scaling group
        @param instance_type [Troposphere.Parameter | string] Reference to the AWS EC2 Instance Type to deploy.
        @param ami_name [string] Name of the AMI to deploy as defined within the RegionMap lookup for the deployed region
        @param ec2_key [Troposphere.Parameter | Troposphere.Ref(Troposphere.Parameter)] Input parameter used to gather the name of the EC2 key to use to secure access to instances launched within this Auto Scaling group
        @param user_data [string[]] Array of strings (lines of bash script) to be set as the user data as a bootstrap script for instances launched within this Auto Scaling group
        @param security_groups [Troposphere.ec2.SecurityGroup[]] array of security groups to be applied to instances within this Auto Scaling group
        @param min_size [int] value to set as the minimum number of instances for the Auto Scaling group
        @param max_size [int] value to set as the maximum number of instances for the Auto Scaling group
        @param root_volume_size [int] size (in GiB) to assign to the root volume of the launched instance
        @param include_ephemerals [Boolean] indicates that ephemeral volumes should be included in the block device mapping of the Launch Configuration
        @param number_ephemeral_vols [int] number of ephemeral volumes to attach within the block device mapping Launch Configuration
        @param ebs_data_volumes [list] dictionary pair of size and type data properties in a list used to create ebs volume attachments
        @param custom_tags [Troposphere.autoscaling.Tag[]] Collection of Auto Scaling tags to be assigned to the Auto Scaling Group
        @param load_balancer [Troposphere.elasticloadbalancing.LoadBalancer] Object reference to an ELB to be assigned to this auto scaling group
        @param instance_monitoring [Boolean] indicates that detailed monitoring should be turned on for all instnaces launched within this Auto Scaling group
        @param subnet_layer [string] string indicating which subnet layer instances are being launched into
        """

        # Ensure that all the passed in parameters are Ref objects
        if ec2_key and type(ec2_key) != Ref:
            ec2_key = Ref(ec2_key)
        elif ec2_key is None:
            ec2_key = Ref(self.parameters['ec2Key'])

        if type(instance_type) != str:
            instance_type = Ref(instance_type)

        sg_list = []
        for sg in security_groups:
            if isinstance(sg, Ref):
                sg_list.append(sg)
            else:
                sg_list.append(Ref(sg))

        # If no instance profile was provided, create one with just the cloudformation read policy
        if not instance_profile:
            instance_profile = self.add_instance_profile(layer_name, [self.get_cfn_policy()], self.name)

        # If subnet_layer isn't passed in, try a private subnet if available, else a public subnet
        if not subnet_layer:
            if len(self._subnets.get('private')) > 0:
                subnet_layer = self._subnets['private'].keys()[0]
            else:
                subnet_layer = self._subnets['public'].keys()[0]

        subnet_type = self.get_subnet_type(subnet_layer)

        # If associate_public_ip is not passed in, set it based on the subnet_type
        if not associate_public_ip:
            associate_public_ip = True if subnet_type == 'public' else False

        launch_config_obj = autoscaling.LaunchConfiguration(
            layer_name + 'LaunchConfiguration',
            IamInstanceProfile=Ref(instance_profile),
            ImageId=FindInMap('RegionMap', Ref('AWS::Region'), ami_name),
            InstanceType=instance_type,
            SecurityGroups=sg_list,
            KeyName=ec2_key,
            AssociatePublicIpAddress=associate_public_ip,
            InstanceMonitoring=instance_monitoring)

        if launch_config_metadata:
            launch_config_obj.Metadata = launch_config_metadata

        if user_data:
            launch_config_obj.UserData = user_data

        block_devices = []
        if root_volume_type and root_volume_size:
            ebs_device = ec2.EBSBlockDevice(
                VolumeSize=root_volume_size)

            if root_volume_type:
                ebs_device.VolumeType = root_volume_type

            block_devices.append(ec2.BlockDeviceMapping(
                DeviceName='/dev/sda1',
                Ebs=ebs_device))

        device_names = ['/dev/sd%s' % c for c in 'bcdefghijklmnopqrstuvwxyz']

        if ebs_data_volumes is not None and len(ebs_data_volumes) > 0:
            for ebs_volume in ebs_data_volumes:
                # Respect names provided by AMI when available
                if 'name' in ebs_volume:
                    device_name = ebs_volume.get('name')
                    device_names.remove(device_name)
                else:
                    device_name = device_names.pop()

                ebs_block_device = ec2.EBSBlockDevice(
                    DeleteOnTermination=ebs_volume.get('delete_on_termination', True),
                    VolumeSize=ebs_volume.get('size', '100'),
                    VolumeType=ebs_volume.get('type', 'gp2'))

                if 'iops' in ebs_volume:
                    ebs_block_device.Iops = int(ebs_volume.get('iops'))
                if 'snapshot_id' in ebs_volume:
                    ebs_block_device.SnapshotId = ebs_volume.get('snapshot_id')

                block_devices.append(ec2.BlockDeviceMapping(
                    DeviceName=device_name,
                    Ebs=ebs_block_device))

        if include_ephemerals and number_ephemeral_vols > 0:
            device_names.reverse()
            for x in range(0, number_ephemeral_vols):
                device_name = device_names.pop()
                block_devices.append(ec2.BlockDeviceMapping(
                    DeviceName=device_name,
                    VirtualName='ephemeral' + str(x)))

        if len(block_devices) > 0:
            launch_config_obj.BlockDeviceMappings = block_devices

        launch_config = self.add_resource(launch_config_obj)

        auto_scaling_obj = autoscaling.AutoScalingGroup(
            layer_name + 'AutoScalingGroup',
            AvailabilityZones=self.azs,
            LaunchConfigurationName=Ref(launch_config),
            MaxSize=max_size,
            MinSize=min_size,
            DesiredCapacity=min(min_size, max_size),
            VPCZoneIdentifier=self.subnets[subnet_type][subnet_layer.lower()],
            TerminationPolicies=['OldestLaunchConfiguration', 'ClosestToNextInstanceHour', 'Default'],
            DependsOn=depends_on)

        lb_tmp = []

        if load_balancer is not None:
            try:
                if type(load_balancer) is dict:
                    for lb in load_balancer:
                        lb_tmp.append(Ref(load_balancer[lb]))
                elif type(load_balancer) is not Ref:
                    for lb in load_balancer:
                        lb_tmp.append(Ref(lb))
                else:
                    lb_tmp.append(load_balancer)
            except TypeError:
                lb_tmp.append(Ref(load_balancer))
        else:
            lb_tmp = None

        if lb_tmp is not None and len(lb_tmp) > 0:
            auto_scaling_obj.LoadBalancerNames = lb_tmp

        if creation_policy is not None:
            auto_scaling_obj.resource['CreationPolicy'] = creation_policy

        if update_policy is not None:
            auto_scaling_obj.resource['UpdatePolicy'] = update_policy

        if custom_tags is not None and len(custom_tags) > 0:
            if type(custom_tags) != list:
                custom_tags = [custom_tags]
            auto_scaling_obj.Tags = custom_tags
        else:
            auto_scaling_obj.Tags = []

        auto_scaling_obj.Tags.append(autoscaling.Tag('Name', layer_name, True))
        return self.add_resource(auto_scaling_obj)

    def add_elb(self, resource_name, ports, utility_bucket=None, instances=[], security_groups=[], ssl_cert_name='', depends_on=[], subnet_layer=None, scheme='internet-facing', health_check_protocol=None, health_check_port=None, health_check_path=''):
        """
        Helper function creates an ELB and attaches it to your template
        Ports should be a dictionary mapping ELB ports to Instance ports
        SSL cert name must be included if using ELB port 443
        TODO: Parameterize more stuff
        """

        # Create default stickiness policy
        # TODO: this could be parameterized
        stickiness_policy_name = '%sElbStickinessPolicy' % resource_name
        stickiness_policy = elb.LBCookieStickinessPolicy(CookieExpirationPeriod='1800', PolicyName=stickiness_policy_name)

        # Add the listeners, setting the correct protocol based on port number
        # Also add the SSL cert if using port 443 (assuming it's already been created and uploaded to IAM)
        listeners = []
        for elb_port in ports:
            if elb_port == tpc.HTTP_PORT:
                listeners.append(elb.Listener(LoadBalancerPort=elb_port, InstancePort=ports[elb_port], Protocol='HTTP', InstanceProtocol='HTTP',
                                 PolicyNames=[stickiness_policy_name]))
            elif elb_port == tpc.HTTPS_PORT:
                listeners.append(elb.Listener(LoadBalancerPort=elb_port, InstancePort=ports[elb_port], Protocol='HTTPS', InstanceProtocol='HTTPS',
                                 SSLCertificateId=Join("", ["arn:aws:iam::", {"Ref": "AWS::AccountId"}, ":server-certificate/", ssl_cert_name]),
                                 PolicyNames=[stickiness_policy_name]))
            else:
                listeners.append(elb.Listener(LoadBalancerPort=elb_port, InstancePort=ports[elb_port], Protocol='TCP', InstanceProtocol='TCP'))

        # If health check port is not passed in, use highest priority available (443 > 80 > anything else)
        if not health_check_port:
            if tpc.HTTPS_PORT in ports:
                health_check_port = ports[tpc.HTTPS_PORT]
            elif tpc.HTTP_PORT in ports:
                health_check_port = ports[tpc.HTTP_PORT]
            else:
                health_check_port = ports.values()[0]

        # If health_check_protocol is not passed in, set it based on the port (443 = HTTPS, 80 = HTTP, otherwise TCP)
        if not health_check_protocol:
            if health_check_port == tpc.HTTPS_PORT:
                health_check_protocol = 'HTTPS'
            elif health_check_port == tpc.HTTP_PORT:
                health_check_protocol = 'HTTP'
            else:
                health_check_protocol = 'TCP'

        health_check_protocol = health_check_protocol.upper()

        if health_check_protocol == 'HTTP' or health_check_protocol == 'HTTPS':
            health_check_target = "%s:%s/%s" % (health_check_protocol, health_check_port, health_check_path.lstrip('/'))
        else:
            health_check_target = "%s:%s" % (health_check_protocol, health_check_port)

        if subnet_layer:
            subnet_type = self.get_subnet_type(subnet_layer)
        else:
            # If subnet layer is not passed in, determine based on the scheme 
            # -- Pick a public subnet if it's internet-facing, else pick a private one
            subnet_type = 'public' if scheme == 'internet-facing' else 'private'
            subnet_layer = self._subnets[subnet_type].keys()[0]

        elb_obj = elb.LoadBalancer(
            '%sElb' % resource_name,
            Subnets=self.subnets[subnet_type][subnet_layer],
            SecurityGroups=[Ref(sg) for sg in security_groups],
            CrossZone=True,
            LBCookieStickinessPolicy=[stickiness_policy],
            HealthCheck=elb.HealthCheck(
                HealthyThreshold=3,
                UnhealthyThreshold=5,
                Interval=30,
                Target=health_check_target,
                Timeout=5),
            Listeners=listeners,
            Instances=instances,
            Scheme=scheme,
            DependsOn=depends_on
        )

        # If an S3 utility bucket was passed in, set up the ELB access log
        if utility_bucket is not None:
            elb_obj.AccessLoggingPolicy = elb.AccessLoggingPolicy(
                EmitInterval=5,
                Enabled=True,
                S3BucketName=utility_bucket)

        return self.add_resource(elb_obj)

    def create_reciprocal_sg(self,
                             source_group,
                             source_group_name,
                             destination_group,
                             destination_group_name,
                             from_port,
                             to_port=None,
                             ip_protocol='tcp'):
        """
        Helper method creates reciprocal ingress and egress rules given two existing security groups and a set of ports
        @param source_group [Troposphere.ec2.SecurityGroup] Object reference to the source security group
        @param source_group_name [string] friendly name of the source security group used for labels
        @param destination_group [Troposphere.ec2.SecurityGroup] Object reference to the destination security group
        @param destination_group_name [string] friendly name of the destination security group used for labels
        @param from_port [string] lower boundary of the port range to set for the secuirty group rules
        @param to_port [string] upper boundary of the port range to set for the security group rules
        @param ip_protocol [string] name of the IP protocol to set this rule for
        """
        if to_port is None:
            to_port = from_port
        if isinstance(from_port, unicode):
            from_port = from_port.encode('ascii', 'ignore')
        if isinstance(to_port, unicode):
            to_port = to_port.encode('ascii', 'ignore')
        if from_port == to_port:
            label_suffix = ip_protocol.capitalize() + str(from_port)
        else:
            label_suffix = ip_protocol.capitalize() + str(from_port) + 'To' + str(to_port)
            
        # A Ref cannot be created from an object that is already a GetAtt
        # and possibly some other CFN types, so expand this list if you discover another one
        CFN_TYPES = [GetAtt]
        if type(source_group) not in CFN_TYPES:
            source_group = Ref(source_group)
        if type(destination_group) not in CFN_TYPES:
            destination_group = Ref(destination_group)

        self.add_resource(ec2.SecurityGroupIngress(
            destination_group_name + 'Ingress' + source_group_name + label_suffix,
            SourceSecurityGroupId=source_group,
            GroupId=destination_group,
            FromPort=from_port,
            ToPort=to_port,
            IpProtocol=ip_protocol))

        self.add_resource(ec2.SecurityGroupEgress(
            source_group_name + 'Egress' + destination_group_name + label_suffix,
            DestinationSecurityGroupId=destination_group,
            GroupId=source_group,
            FromPort=from_port,
            ToPort=to_port,
            IpProtocol=ip_protocol))

    def get_cfn_policy(self):
        """
        Helper method returns the standard IAM policy to allow cloudformation read actions
        """
        return iam.Policy(
            PolicyName='cloudformationRead',
            PolicyDocument={
                "Statement": [{
                    "Effect": "Allow",
                    "Action": [
                        "cloudformation:DescribeStackEvents",
                        "cloudformation:DescribeStackResource",
                        "cloudformation:DescribeStackResources",
                        "cloudformation:DescribeStacks",
                        "cloudformation:ListStacks",
                        "cloudformation:ListStackResources"],
                    "Resource": "*"}]
            })

    def register_elb_to_dns(self,
                            elb,
                            tier_name,
                            tier_args):
        """
        Method handles the process of uniformly creating CNAME records for ELBs in a given tier
        @param elb [Troposphere.elasticloadbalancing.LoadBalancer]
        @param tier_name [str]
        @param tier_args [dict]
        """
        if 'environmentHostedZone' not in self.parameters:
            hostedzone = self.add_parameter(Parameter(
                "environmentHostedZone",
                Description="The DNS name of an existing Amazon Route 53 hosted zone",
                Default=tier_args.get('base_hosted_zone_name', 'devopsdemo.com'),
                Type="String"))
        else:
            hostedzone = self.parameters.get('environmentHostedZone')

        if tier_name.lower() + 'HostName' not in self.parameters:
            host_name = self.add_parameter(Parameter(
                tier_name.lower() + 'HostName',
                Description="Friendly host name to append to the environmentHostedZone base DNS record",
                Type="String",
                Default=tier_args.get('tier_host_name', tier_name.lower())))
        else:
            host_name = self.parameters.get(tier_name.lower() + 'HostName')

        self.add_resource(r53.RecordSetType(
            tier_name.lower() + 'DnsRecord',
            HostedZoneName=Join('', [Ref(hostedzone), '.']),
            Comment='CNAME record for ' + tier_name.capitalize() + ' tier',
            Name=Join('', [Ref(host_name), '.', Ref(hostedzone)]),
            Type='CNAME',
            TTL='300',
            ResourceRecords=[GetAtt(elb, 'DNSName')]))

    def get_logging_bucket_policy_document(self,
                                           utility_bucket,
                                           elb_log_prefix='elb_logs',
                                           cloudtrail_log_prefix='cloudtrail_logs'):
        """
        Method builds the S3 bucket policy statements which will allow the proper AWS account ids to write ELB Access Logs to the specified bucket and prefix.
        Per documentation located at: http://docs.aws.amazon.com/ElasticLoadBalancing/latest/DeveloperGuide/configure-s3-bucket.html
        @param utility_bucket [Troposphere.s3.Bucket] object reference of the utility bucket for this tier
        @param elb_log_prefix [string] prefix for paths used to prefix the path where ELB will place access logs
        """
        if elb_log_prefix != None and elb_log_prefix != '':
            elb_log_prefix = elb_log_prefix + '/'
        else:
            elb_log_prefix = ''

        if cloudtrail_log_prefix != None and cloudtrail_log_prefix != '':
            cloudtrail_log_prefix = cloudtrail_log_prefix + '/'
        else:
            cloudtrail_log_prefix = ''

        elb_accts = {'us-west-1': '027434742980',
                     'us-west-2': '797873946194',
                     'us-east-1': '127311923021',
                     'eu-west-1': '156460612806',
                     'ap-northeast-1': '582318560864',
                     'ap-southeast-1': '114774131450',
                     'ap-southeast-2': '783225319266',
                     'sa-east-1': '507241528517',
                     'us-gov-west-1': '048591011584'}

        for region in elb_accts:
            self.add_region_map_value(region, 'elbAccountId', elb_accts[region])

        # The principal account IDs in the following statements refer to the AWS CloudTrail account IDs
        # They explicitly need write permissions in order to upload logs to your bucket
        statements = [{
            "Action": ["s3:PutObject"],
            "Effect": "Allow",
            "Resource": Join('', ['arn:aws:s3:::', utility_bucket, '/', elb_log_prefix + 'AWSLogs/', Ref('AWS::AccountId'), '/*']),
            "Principal": {"AWS": [FindInMap('RegionMap', Ref('AWS::Region'), 'elbAccountId')]}},
            {
                "Action": ["s3:GetBucketAcl"],
                "Resource": Join('', ["arn:aws:s3:::", utility_bucket]),
                "Effect": "Allow",
                "Principal": {
                    "AWS": [
                        "arn:aws:iam::903692715234:root",
                        "arn:aws:iam::859597730677:root",
                        "arn:aws:iam::814480443879:root",
                        "arn:aws:iam::216624486486:root",
                        "arn:aws:iam::086441151436:root",
                        "arn:aws:iam::388731089494:root",
                        "arn:aws:iam::284668455005:root",
                        "arn:aws:iam::113285607260:root"]}},
            {
                "Action": ["s3:PutObject"],
                "Resource": Join('', ["arn:aws:s3:::", utility_bucket, '/', cloudtrail_log_prefix + "AWSLogs/", Ref("AWS::AccountId"), '/*']),
                "Effect": "Allow",
                "Principal": {
                    "AWS": [
                        "arn:aws:iam::903692715234:root",
                        "arn:aws:iam::859597730677:root",
                        "arn:aws:iam::814480443879:root",
                        "arn:aws:iam::216624486486:root",
                        "arn:aws:iam::086441151436:root",
                        "arn:aws:iam::388731089494:root",
                        "arn:aws:iam::284668455005:root",
                        "arn:aws:iam::113285607260:root"]},
                    "Condition": {"StringEquals": {"s3:x-amz-acl": "bucket-owner-full-control"}}}]

        self.add_output(Output(
            'elbAccessLoggingBucketAndPath',
            Value=Join('', ['arn:aws:s3:::', utility_bucket, elb_log_prefix]),
            Description='S3 bucket and key name prefix to use when configuring elb access logs to aggregate to S3'))

        self.add_output(Output(
            'cloudTrailLoggingBucketAndPath',
            Value=Join('', ['arn:aws:s3:::', utility_bucket, cloudtrail_log_prefix]),
            Description='S3 bucket and key name prefix to use when configuring CloudTrail to aggregate logs to S3'))

        return {"Statement": statements}

    def create_vpcflowlogs_role(self):
        flowlogs_policy = aws.Policy(
            Version="2012-10-17",
            Statement=[
                aws.Statement(
                    Sid="",
                    Effect=aws.Allow,
                    Resource=['*'],
                    Action=[awacs_logs.CreateLogGroup,
                            awacs_logs.CreateLogStream,
                            awacs_logs.PutLogEvents,
                            awacs_logs.DescribeLogGroups,
                            awacs_logs.DescribeLogStreams],
                )
            ]
        )

        flowlogs_trust_policy = aws.Policy(
            Version="2012-10-17",
            Statement=[make_simple_assume_statement("vpc-flow-logs.amazonaws.com")]
        )

        vpcflowlogs_role = iam.Role(
            'VPCFlowLogsIAMRole',
            AssumeRolePolicyDocument=flowlogs_trust_policy,
            Path='/',
            Policies=[
                iam.Policy(PolicyName='vpcflowlogs_policy', PolicyDocument=flowlogs_policy)
            ])

        return vpcflowlogs_role

    def add_utility_bucket(self, name=None):
        """
        Method adds a bucket to be used for infrastructure utility purposes such as backups
        @param name [str] friendly name to prepend to the CloudFormation asset name
        """
        if name:
            self._utility_bucket = name
        else:
            self._utility_bucket = self.add_resource(s3.Bucket(
                name.lower() + 'UtilityBucket',
                AccessControl=s3.BucketOwnerFullControl,
                DeletionPolicy=Retain))

            bucket_policy_statements = self.get_logging_bucket_policy_document(
                self.utility_bucket,
                elb_log_prefix=res.get_str('elb_log_prefix', ''),
                cloudtrail_log_prefix=res.get_str('cloudtrail_log_prefix', ''))

            self.add_resource(s3.BucketPolicy(
                name.lower() + 'UtilityBucketLoggingPolicy',
                Bucket=self.utility_bucket,
                PolicyDocument=bucket_policy_statements))

        log_group_name = 'DefaultLogGroup'
        self.add_resource(logs.LogGroup(
            log_group_name,
            RetentionInDays=7
        ))

        self.add_resource(self.create_vpcflowlogs_role())

        self.manual_parameter_bindings['utilityBucket'] = self.utility_bucket

    def add_child_template(self, child_template, merge=False, depends_on=[]):
        child_template_entry = (child_template, merge, depends_on)
        self._child_templates.append(child_template_entry)

    def process_child_templates(self):
        stack_outputs = {}
        for (child_template, merge, depends_on) in self._child_templates:

            self.process_child_template(child_template, merge, depends_on)

            # # TODO: output autowiring feature, disambiguation of output sources
            # for output in child_template.outputs:
            #     stack_outputs[output.name] = child_template


    def match_stack_parameters(self, child_template):
        stack_params = {}

        for parameter in child_template.parameters.keys():

            # Manual parameter bindings single-namespace
            if parameter in self.manual_parameter_bindings:
                manual_match = self.manual_parameter_bindings[parameter]
                stack_params[parameter] = manual_match

            # Naming scheme for identifying the AZ of a subnet
            elif parameter.startswith('availabilityZone'):
                index = int(parameter[-1:])
                stack_params[parameter] = Select(index, t.GetAZs(Ref(t.AWS_REGION)))
                # stack_params[parameter] = GetAtt('privateSubnet' + parameter.replace('availabilityZone', ''), 'AvailabilityZone')

            # Match any child stack parameters that have the same name as this stacks **parameters**
            elif parameter in self.parameters.keys():
                param_match = self.parameters.get(parameter)
                stack_params[parameter] = Ref(param_match)

            # Match any child stack parameters that have the same name as this stacks **resources**
            elif parameter in self.resources.keys():
                resource_match = self.resources.get(parameter)
                stack_params[parameter] = Ref(resource_match)

            # # Match any child stack parameters that have the same name as a top-level **stack_output**
            # TODO: Enable Output autowiring
            # elif parameter in self.stack_outputs:
            #     stack_params[parameter] = GetAtt(self.stack_outputs[parameter], 'Outputs.' + parameter)

            # Finally if nothing else matches copy the child templates parameter to this template's parameter list
            # so the value will pass through this stack down to the child.
            else:
                new_param = self.add_parameter(child_template.parameters[parameter])
                stack_params[parameter] = Ref(new_param)

        return stack_params

    def process_child_template(self, child_template, merge, depends_on):
        if merge:
            self.merge(child_template)
            return

        child_template.add_common_parameters_from_parent(self)
        child_template.build_hook()

        # assemble parameters
        stack_params = self.match_stack_parameters(child_template)

        # assemble template path
        full_s3_path = utility.template_s3_url(Template.template_bucket, child_template.resource_path)

        # create stack
        stack_obj = cf.Stack(
            child_template.name + 'Stack',
            TemplateURL=full_s3_path,
            Parameters=stack_params,
            TimeoutInMinutes=Template.stack_timeout,
            DependsOn=depends_on)

        return self.add_resource(stack_obj)

    # Return the subnet type (public/private) that subnet_layer belongs to
    def get_subnet_type(self, subnet_layer):
        for subnet_type in self._subnets:
            for a_subnet_layer in self._subnets[subnet_type]:
                if a_subnet_layer == subnet_layer:
                    return subnet_type
        return None

