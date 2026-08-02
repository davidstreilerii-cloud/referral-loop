"""The domain layer.

Nothing in here imports the store, the transport, the matcher, or the FHIR projection.
That is the property the whole layering rests on: the same model has to be reachable from
an MLLP listener, a CDS Hooks service and a batch job, and it stops being reachable the
moment it needs a database connection to be constructed.

Enforced by tests/test_import_closure.py, in a clean subprocess -- an in-process check
would pass vacuously once anything else in the suite had already imported the store.
"""
