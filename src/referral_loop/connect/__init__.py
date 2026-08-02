"""Who we call, how we prove ourselves, and what we may believe back.

peers.py settled the same question for inbound traffic -- whose message is this, and what is
that peer allowed to assert. It answered that a self-asserted origin is not an origin: identity
comes from a credential the sender cannot choose, and authority is granted per peer rather than
inferred from the message type.

This package is that decision pointed outbound. A FHIR endpoint we read from is asserting things
exactly as an MLLP peer does -- if a fetched DocumentReference can close a loop, that endpoint
just exercised `result` -- so it draws from the same authority vocabulary and refuses the boot
on the same kind of missing value.

egress.py is the only module in the repository permitted to import urllib.request. That is
enforced by an AST test rather than by convention, because it is the property the narrowed
README claim rests on: no model calls, and egress only to configured connectors.
"""
