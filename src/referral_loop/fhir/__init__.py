"""The projection layer: canonical model out to FHIR.

One direction only. core/ knows nothing about FHIR (tests/test_import_closure.py holds it
to that), because the model deliberately carries distinctions Task.status cannot express --
if the domain layer imported this package the pressure would run the other way and the
vocabulary would drift back towards the one the wire happens to use.

Nothing here reads a store or a clock either. A projection takes a domain object and
returns a resource-shaped dict, which is what makes the same function callable from the
worklist, from a batch export and from a CDS Hooks response.
"""
