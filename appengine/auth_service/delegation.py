# Copyright 2015 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""API handler to mint delegation tokens."""

import logging
import webapp2

from google.appengine.ext import ndb

from components import auth
from components import utils

from components.auth import delegation
from components.auth import ipaddr
from components.auth.proto import delegation_pb2

from proto import config_pb2

import config


# Minimum accepted value for 'validity_duration'.
MIN_VALIDITY_DURATION_SEC = 30

# Maximum accepted value for 'validity_duration'. May be constrained further
# by rules in delegation.cfg. See 'max_validity_duration'.
MAX_VALIDITY_DURATION_SEC = 24 * 3600

# How long delegation token is valid if 'validity_duration' was not provided.
DEF_VALIDITY_DURATION_SEC = 3600


def get_rest_api_routes():
  """Routes exposed by auth_service frontend."""
  return [
    webapp2.Route(
      r'/auth_service/api/v1/delegation/token/create',
      CreateDelegationTokenHandler),
  ]


class CreateDelegationTokenHandler(auth.ApiHandler):
  """API endpoint to create a new delegation token.

  The POST request body describes who delegates what authority to whom where:
  {
    # To WHOM the authority is delegated.
    #
    # Each item can be an identity string (e.g. "user:<email>"),
    # a "group:<name>" string, or special '*' string which means "Any bearer can
    # use the token".
    #
    # Required.
    'audience': ['user:def@example.com', 'group:abcdef'],

    # WHERE the token is valid.
    #
    # List of services (specified as service identities, e.g. "service:app-id")
    # that should accept this token. May also contain special '*' string, which
    # means "All services".
    #
    # Required.
    'services': ['service:gae-app1', 'service:gae-app2'],

    # How long the token is valid after creation (in seconds). Default is 1h.
    'validity_duration': 3600,

    # A caller can mint a delegation token on some else's behalf (effectively
    # impersonating them). Only a privileged set of callers can do that.
    # If impersonation is allowed, token's delegated_identity field will contain
    # whatever is in 'impersonate' field. See DelegationConfig in
    # proto/config.proto.
    'impersonate': 'user:abc@example.com',

    # Optional reason why the token is created. Used only for logging purposes,
    # will be indexed. Should be a short identifier-like string.
    'intent': 'for-doing-stuff'
  }

  Response is:
  {
    'delegation_token': '<urlsafe base64 encoded blob with delegation token>',
    'subtoken_id': '<int64>', # as string
    'validity_duration': 3600
  }
  """

  @auth.require(lambda: not auth.get_current_identity().is_anonymous)
  def post(self):
    # Forbid usage of delegation tokens for this particular call. Using
    # delegation when creating delegation tokens is too deep. Redelegation will
    # be done as separate explicit API call that accept existing delegation
    # token via request body, not via headers.
    if auth.get_current_identity() != auth.get_peer_identity():
      raise auth.AuthorizationError(
          'This API call must not be used with active delegation token')

    # Convert request body to proto (with validation). Verify IP format.
    try:
      body = self.parse_body()
      subtoken = subtoken_from_jsonish(body)
      intent = body.get('intent') or ''
      if not isinstance(intent, basestring):
        raise TypeError('"intent" must be string')
    except (TypeError, ValueError) as exc:
      self.abort_with_error(400, text=str(exc))

    # Fill in defaults.
    assert not subtoken.requestor_identity
    user_id = auth.get_current_identity().to_bytes()
    subtoken.requestor_identity = user_id
    if not subtoken.delegated_identity:
      subtoken.delegated_identity = user_id
    subtoken.creation_time = int(utils.time_time())
    if not subtoken.validity_duration:
      subtoken.validity_duration = DEF_VALIDITY_DURATION_SEC
    if '*' in subtoken.services:
      subtoken.services[:] = get_default_allowed_services(user_id)

    # Check ACL (raises auth.AuthorizationError on errors).
    rule = check_can_create_token(user_id, subtoken)

    # Register the token in the datastore, generate its ID.
    subtoken.subtoken_id = register_subtoken(
        subtoken, rule, intent, auth.get_peer_ip())

    # Create and sign the token.
    try:
      token = delegation.serialize_token(delegation.seal_token(subtoken))
    except delegation.BadTokenError as exc:
      # This happens if resulting token is too large.
      self.abort_with_error(400, text=str(exc))

    self.send_response(
        response={
          'delegation_token': token,
          'subtoken_id': str(subtoken.subtoken_id),
          'validity_duration': subtoken.validity_duration,
        },
        http_code=201)


def subtoken_from_jsonish(d):
  """Given JSON dict with request body returns delegation_pb2.Subtoken msg.

  Raises:
    ValueError if some fields are invalid.
  """
  msg = delegation_pb2.Subtoken()

  # 'audience' is a required list of 'group:...', identity names or '*'.
  if 'audience' in d:
    aud = d['audience']
    if not isinstance(aud, list):
      raise ValueError('"audience" must be a non-empty list of strings')
    for e in aud:
      if not isinstance(e, basestring):
        raise ValueError('"audience" must be a non-empty list of strings')
      if e.startswith('group:'):
        if not auth.is_valid_group_name(e[len('group:'):]):
          raise ValueError('Invalid group name in "audience": %s' % e)
      elif e != '*':
        try:
          auth.Identity.from_bytes(e)
        except ValueError as exc:
          raise ValueError(
              'Invalid identity name "%s" in "audience": %s' % (e, exc))
      msg.audience.append(str(e))
    if '*' in msg.audience:
      msg.audience[:] = ['*']

  if not msg.audience:
    raise ValueError('"audience" must be a non-empty list of strings')

  # 'services' is a required list of identity names.
  if 'services' in d:
    services = d['services']
    if not isinstance(services, list):
      raise ValueError('"services" must be a non-empty list of strings')
    for e in services:
      if not isinstance(e, basestring):
        raise ValueError('"services" must be a non-empty list of strings')
      if e != '*':
        try:
          auth.Identity.from_bytes(e)
        except ValueError as exc:
          raise ValueError(
              'Invalid identity name "%s" in "services": %s' % (e, exc))
      msg.services.append(str(e))
    if '*' in msg.services:
      msg.services[:] = ['*']

  if not msg.services:
    raise ValueError('"services" must be a non-empty list of strings')

  # 'validity_duration' is optional positive number within some defined bounds.
  if 'validity_duration' in d:
    dur = d['validity_duration']
    if not isinstance(dur, (int, float)):
      raise ValueError('"validity_duration" must be a positive number')
    if dur < MIN_VALIDITY_DURATION_SEC or dur > MAX_VALIDITY_DURATION_SEC:
      raise ValueError(
          '"validity_duration" must be between %d and %d sec' %
          (MIN_VALIDITY_DURATION_SEC, MAX_VALIDITY_DURATION_SEC))
    msg.validity_duration = int(dur)

  # 'impersonate' is an optional identity string.
  if 'impersonate' in d:
    imp = d['impersonate']
    try:
      auth.Identity.from_bytes(imp)
    except ValueError as exc:
      raise ValueError(
          'Invalid identity name "%s" in "impersonate": %s' % (imp, exc))
    msg.delegated_identity = str(imp)

  return msg


################################################################################


# Fallback rule returned if nothing else matches.
DEFAULT_RULE = config_pb2.DelegationConfig.Rule(
    user_id=['*'],
    target_service=['*'],
    max_validity_duration=MAX_VALIDITY_DURATION_SEC)


def get_delegation_rule(user_id, services):
  """Returns first matching rule from delegation.cfg DelegationConfig rules.

  Args:
    user_id: identity string to match against 'user_id' field.
    services: list of identities (as strings) to match against 'target_service'.
      If contains '*', first user_id-matching rule will be returned.

  Returns:
    config_pb2.DelegationConfig.Rule if found, DEFAULT_RULE if not.
  """
  ident = auth.Identity.from_bytes(user_id)
  services_set = set(services)
  for r in config.get_delegation_config().rules:
    if any(is_identity_in_principal_set(ident, p) for p in r.user_id):
      if ('*' in r.target_service or
          '*' in services or
          services_set.issubset(r.target_service)):
        return r
  return DEFAULT_RULE


def is_identity_in_principal_set(ident, principals):
  """True if Identity is in a set specified by principals.

  Args:
    ident: auth.Identity instance.
    principals: string, one of 'group:<name>', identity glob string ('user:*'),
        a single identity ('user:abc@example.com'), or '*' (which stands for
        'everyone', including anonymous).

  Returns:
    True or False. Also returns False if 'principals' is in unrecognized format.
  """
  try:
    # Everyone?
    if principals == '*':
      return True
    # Group?
    if principals.startswith('group:'):
      return auth.is_group_member(principals[len('group:'):], ident)
    # Glob?
    if '*' in principals:
      return auth.IdentityGlob.from_bytes(principals).match(ident)
    # Identity?
    return auth.Identity.from_bytes(principals) == ident
  except ValueError as ex:
    logging.error('Unrecognized principal string "%s": %s', principals, ex)
    return False


def check_can_create_token(user_id, subtoken):
  """Checks that caller is allowed to mint a given root token.

  Args:
    user_id: identity string of a current caller.
    subtoken: instance of delegation_pb2.Subtoken describing root token.

  Returns:
    config_pb2.DelegationConfig.Rule that allows the operation.

  Raises:
    auth.AuthorizationError if such token is not allowed for the caller.
  """
  rule = get_delegation_rule(user_id, subtoken.services)

  if subtoken.validity_duration > rule.max_validity_duration:
    raise auth.AuthorizationError(
        'Maximum allowed validity_duration is %d sec, %d requested.' %
        (rule.max_validity_duration, subtoken.validity_duration))

  # Just delegating one's own identity (not impersonating someone else)? Allow.
  if subtoken.delegated_identity == user_id:
    return rule

  # Verify it's OK to impersonate a given user.
  impersonated = auth.Identity.from_bytes(subtoken.delegated_identity)
  for principal_set in rule.allowed_to_impersonate:
    if is_identity_in_principal_set(impersonated, principal_set):
      return rule

  raise auth.AuthorizationError(
      '"%s" is not allowed to impersonate "%s" on %s' %
      (user_id, subtoken.delegated_identity, subtoken.services or ['*']))


def get_default_allowed_services(user_id):
  """Returns the list of services defined by a first matching rule.

  Args:
    user_id: identity string of a current caller.
  """
  rule = get_delegation_rule(user_id, ['*'])
  return rule.target_service


################################################################################


class AuthDelegationSubtoken(ndb.Model):
  """Represents a delegation subtoken.

  Used to track what tokens are issued. Root entity. ID is autogenerated.
  """
  # Serialized delegation_pb2.Subtoken proto.
  subtoken = ndb.BlobProperty()
  # Serialized config_pb2.DelegationConfig.Rule that allowed this token.
  rule = ndb.BlobProperty()
  # Intent supplied when creating the token.
  intent = ndb.StringProperty()
  # IP address the minting request came from.
  caller_ip = ndb.StringProperty()
  # Version of the auth_service that created the subtoken.
  auth_service_version = ndb.StringProperty()

  # Fields below are extracted from 'subtoken', for indexing purposes.

  # Whose authority the token conveys.
  delegated_identity = ndb.StringProperty()
  # When the token was created.
  creation_time = ndb.DateTimeProperty()
  # List of services that accept the token (or ['*'] if all).
  services = ndb.StringProperty(repeated=True)
  # Who initiated the minting request.
  requestor_identity = ndb.StringProperty()


def register_subtoken(subtoken, rule, intent, caller_ip):
  """Creates new AuthDelegationSubtoken entity in the datastore, returns its ID.

  Args:
    subtoken: delegation_pb2.Subtoken describing the token.
    intent: intent supplied when creating the token.
    rule: config_pb2.DelegationConfig.Rule that allows the operation
    caller_ip: ipaddr.IP of the caller.

  Returns:
    int64 with ID of the new entity.
  """
  entity = AuthDelegationSubtoken(
      subtoken=subtoken.SerializeToString(),
      rule=rule.SerializeToString(),
      intent=intent,
      caller_ip=ipaddr.ip_to_string(caller_ip),
      auth_service_version=utils.get_app_version(),
      delegated_identity=subtoken.delegated_identity,
      creation_time=utils.timestamp_to_datetime(subtoken.creation_time*1e6),
      services=list(subtoken.services),
      requestor_identity=subtoken.requestor_identity)
  entity.put(use_cache=False, use_memcache=False)
  subtoken_id = entity.key.integer_id()

  # Keep a logging entry (extractable via BigQuery) too.
  logging.info(
      'subtoken: subtoken_id=%d caller_ip=%s '
      'delegated_identity=%s requestor_identity=%s',
      subtoken_id, entity.caller_ip,
      entity.delegated_identity, entity.requestor_identity)

  return subtoken_id
