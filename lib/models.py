import pdb
from peewee import *
from pprint import pprint
from time import time
import simplejson
import binascii
import re

import sys, os
sys.path.append( os.path.join( os.path.dirname(__file__), '..' ) )
sys.path.append( os.path.join( os.path.dirname(__file__), '..' , 'lib' ) )

import config
import misc
import dashd

# our mixin
from queue_gov_object import QueueGovObject
from dashlib import is_valid_dash_address


env = os.environ.get('SENTINEL_ENV') or 'production'
db_cfg = config.db[env].copy()
dbname = db_cfg.pop('database')

db = MySQLDatabase(dbname, **db_cfg)

# === models ===

class BaseModel(Model):
    def get_dict(self):
        dikt = {}
        for field_name in self._meta.columns.keys():

            # don't include DB id
            if "id" == field_name:
                continue

            dikt[ field_name ] = getattr( self, field_name )

        # dashd shim (overall system needs design review)
        try:
            dashd_go_type = getattr( self, 'govobj_type' )
            dikt[ 'type' ] = dashd_go_type
        except:
            pass

        return dikt

    class Meta:
        database = db

    def is_valid(self):
        raise NotImplementedError("Method be over-ridden in subclasses")

class GovernanceObject(BaseModel):
    #id = IntegerField(primary_key = True)
    parent_id = IntegerField(default=0)
    object_creation_time = IntegerField(default=int(time()))
    object_hash = CharField(default='0')
    object_parent_hash = CharField(default='0')
    object_name = CharField(default='')
    object_type = IntegerField(default=0)
    object_revision = IntegerField(default=1)
    object_fee_tx = CharField(default='')

    class Meta:
        db_table = 'governance_objects'

    subclasses = ['proposals', 'superblocks']

    @classmethod
    def root(self):
        root_properties = {
            "object_name" : "root",
            "object_type" : 0,
            "object_creation_time" : 0,
        }
        return self(**root_properties)

    @classmethod
    def object_with_name_exists(self, name):
        count = self.select().where(self.object_name == name).count()
        return count > 0

    @property
    def object_data(self):
        return self.serialize_subclasses()

    def serialize_subclasses(self):
        import inflection
        objects = []

        for obj_type in self._meta.reverse_rel.keys():
            if obj_type in self.subclasses:
                res = getattr( self, obj_type )
                if res:
                    # should only return one row, but for completeness...
                    for row in res:
                        # dashd shim
                        dashd_type = inflection.singularize(obj_type)
                        if obj_type == 'superblock':
                            dashd_type = 'trigger'

                        objects.append((dashd_type, row.get_dict()))

        the_json = simplejson.dumps(objects, sort_keys = True)
        the_hex = binascii.hexlify( the_json )

        return the_hex

    def get_prepare_command(self):
        cmd = "gobject prepare %s %s %s %s %s" % (
            self.object_parent_hash,
            self.object_revision,
            self.object_creation_time,
            self.object_name,
            self.object_data
        )
        return cmd

    def get_submit_command(self):
        cmd = "gobject submit %s %s %s %s %s %s" % (
            self.object_fee_tx,
            self.object_parent_hash,
            self.object_revision,
            self.object_creation_time,
            self.object_name,
            self.object_data
        )
        return cmd

    def vote(self):
        # TODO
        pass

    def is_valid(self):
        raise NotImplementedError("Method be over-ridden in subclasses")
        # -- might be possible to do base checks here and then ...
        # govobj.is_valid() in sub-classes (as an alternative "super" since
        # they're not true Python sub-classes)
        """
            - check tree position validity
            - check signatures of owners
            - check validity of revision (must be n+1)
            - check validity of field data (address format, etc)
        """

    @classmethod
    def load_from_dashd(self, rec):
        import inflection
        # http://docs.peewee-orm.com/en/latest/peewee/querying.html#create-or-get
        # user, created = User.get_or_create(username=username)

        # first pick out vars... then try and find/create? then return new obj?
        subobject_hex = rec['DataHex']
        object_name = rec['Name']
        gobj_dict = {
            'object_hash': rec['Hash'],
            'object_fee_tx': rec['CollateralHash'],
            'object_name': object_name,
        }

        objects = simplejson.loads( binascii.unhexlify(subobject_hex), use_decimal=True )
        subobj = None


        # for obj in objects:
        # will there ever be multiple? -- just this for now
        obj = objects[0]

        (dashd_type, dikt) = obj[0:2:1]
        obj_type = dashd_type

        # sigh. reverse-shim this back...
        if dashd_type == 'trigger':
            obj_type = 'superblock'

        obj_type = inflection.pluralize(obj_type)
        subclass = self._meta.reverse_rel[obj_type].model_class

        # exclude any invalid model data from dashd...
        valid_keys = subclass._meta.columns.keys()
        if 'id' in valid_keys: valid_keys.remove('id') # minus 'id'...
        subdikt = { k: dikt[k] for k in valid_keys if k in dikt }

        # sigh. set name (even tho redundant in DB...)
        subdikt['name'] = object_name

        # govobj = self(**gobj_dict)
        # subobj = subclass(**subdikt)
        # subobj.governance_object = govobj


        govobj, created = self.get_or_create(object_hash=gobj_dict['object_hash'], defaults=gobj_dict)
        # print "govobj hash = %s" % gobj_dict['object_hash']
        # print "govobj created = %s" % created

        subdikt['governance_object'] = govobj

        # -- workaround 'til we can rename to just 'name' in proposal, subobject
        goc_dikt = {
          subclass.name_field: object_name,
          'defaults': subdikt,
        }
        subobj, created = subclass.get_or_create(**goc_dikt)
        # print "subobj name = %s" % object_name
        # print "subobj created = %s" % created
        # print "=" * 72

        # ATM, returns a tuple w/govobj and the subobject
        return (govobj, subobj)

    # return an array of invalid GO's
    @classmethod
    def invalid(self):
        return [go for go in self.select() if not go.is_valid()]

class Action(BaseModel):
    #id = IntegerField(primary_key = True)
    #governance_object_id = IntegerField(unique=True)
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'actions')
    absolute_yes_count = IntegerField()
    yes_count = IntegerField()
    no_count = IntegerField()
    abstain_count = IntegerField()
    class Meta:
        db_table = 'actions'

class Event(BaseModel):
    #id = IntegerField(primary_key = True)
    #governance_object_id = IntegerField(unique=True)
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'events')
    start_time = IntegerField(default=int(time()))
    prepare_time = IntegerField()
    submit_time = IntegerField()
    error_time = IntegerField()
    error_message = CharField()

    class Meta:
        db_table = 'events'

    @classmethod
    def new(self):
        return self.select().where(
            (self.start_time <= misc.get_epoch() ) &
            (self.error_time == 0) &
            (self.prepare_time == 0)
        )

    @classmethod
    def prepared(self):
        now = misc.get_epoch()

        return self.select().where(
            (self.start_time <= now ) &
            (self.prepare_time <= now ) &
            (self.prepare_time > 0 ) &
            (self.submit_time == 0)
        )

    @classmethod
    def submitted(self):
        now = misc.get_epoch()

        return self.select().where(
            (self.submit_time > 0 )
        )

class Setting(BaseModel):
    #id = IntegerField(primary_key = True)
    datetime = IntegerField()
    setting  = CharField()
    name     = CharField()
    value    = CharField()
    class Meta:
        db_table = 'settings'

class Proposal(BaseModel, QueueGovObject):
    import dashlib
    #id = IntegerField(primary_key = True)
    #governance_object_id = IntegerField(unique=True)
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'proposals')
    proposal_name = CharField(unique=True)
    start_epoch = IntegerField()
    end_epoch = IntegerField()
    payment_address = CharField()
    payment_amount = DecimalField(max_digits=16, decimal_places=8)

    # TODO: remove this redundancy if/when dashd can be fixed to use
    # strings/types instead of ENUM types for type ID
    govobj_type = 1

    # TODO: rename column 'proposal_name' to 'name' and remove this
    name_field = 'proposal_name'

    class Meta:
        db_table = 'proposals'

    # TODO: rename column 'proposal_name' to 'name' and remove this
    @property
    def name(self):
        return self.proposal_name

    @name.setter
    def name(self, value):
        self.proposal_name = value

    # TODO: unit tests for all these items, both individually and some grouped
    # **This can be easily mocked.**
    def is_valid(self):
        now = misc.get_epoch()

        # proposal name is normalized (something like "[a-zA-Z0-9-_]+")
        if not re.match( '^[-_a-zA-Z0-9]+$', self.name ):
            return False

        # end date < start date
        if ( self.end_epoch <= self.start_epoch ):
            return False

        # end date < current date
        if ( self.end_epoch <= now ):
            return False

        # TODO: consider a mixin for this class's dashd calls -- that or a
        # global... need to find an elegant way to handle this...
        #
        # TODO: get a global dashd instance, or something... gotta query the
        #       dashd for the budget allocation here...
        #
        # TODO: dashlib.get_superblock_budget_allocation should be memoized for
        #       each run of this... no sense in calling it multiple times over
        #       one or two seconds...
        max_budget_allocation = dashlib.get_superblock_budget_allocation(TODOdashd)
        if ( self.payment_amount > max_budget_allocation ):
            return False

        if ( self.payment_amount <= 0 ):
            return False

        # payment address is valid base58 dash addr, non-multisig
        if not is_valid_dash_address( self.payment_address, config.network ):
            return False

        return True


    def is_deletable(self):
        # end_date < (current_date - 30 days)
        thirty_days = (86400 * 30)
        if ( self.end_epoch < (misc.get_epoch() - thirty_days) ):
            return True

        # TBD (item moved to external storage/DashDrive, etc.)
        return False


    @classmethod
    def approved_and_ranked(self, event_block_height, proposal_quorum):
        pass
        # -- see Tyler's implementation of proposal_quorum
        # govinfo = dashd.rpc_command( 'getgovernanceinfo' )
        # govinfo['governanceminquorum']
        # Inject this parameter however it will be done...
        # Minimum number of absolute yes votes to include a proposal in a superblock
        #PROPOSAL_QUORUM = 10
        # TODO: Should be calculated based on the number of masternodes
        # with an absolute minimum of 10 (maybe 1 for testnet)
        # ie. max( 10, (masternode count)/10 )
        PROPOSAL_QUORUM = 0

        # return all approved proposals, in order of descending vote count
        # get rank for each from dashd... probably from regular sync

        ranked = []
        for proposal in Proposal.select():
            if ( proposal.is_valid() ):
                ranked.append( proposal )

        # now order array by vote rank
        return ranked

class Superblock(BaseModel, QueueGovObject):
    #id = IntegerField(primary_key = True)
    #governance_object_id = IntegerField(unique=True)
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'superblocks')
    superblock_name      = CharField() # unique?
    event_block_height   = IntegerField()
    payment_addresses    = TextField()
    payment_amounts      = TextField()

    # TODO: remove this redundancy if/when dashd can be fixed to use
    # strings/types instead of ENUM types for type ID
    govobj_type = 2

    # TODO: rename column 'superblock_name' to 'name' and remove this
    name_field = 'superblock_name'

    class Meta:
        db_table = 'superblocks'

    # TODO: rename column 'superblock_name' to 'name' and remove this
    @property
    def name(self):
        return self.superblock_name

    @name.setter
    def name(self, value):
        self.superblock_name = value

    def is_valid(self):
        # vout != generated vout
        # blockheight != generated blockheight
        pass

    def is_deletable(self):
        # end_date < (current_date - 30 days)
        # TBD (item moved to external storage/DashDrive, etc.)
        pass

    @classmethod
    def valid(self):
        return [sb for sb in self.select() if sb.is_valid()]


# === /models ===

db.connect()
