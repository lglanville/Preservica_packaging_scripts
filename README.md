# Preservica SIP library

Python library and command line scripts for building Preservica V6 Submission
Information packages (SIPs). Preservica 6 has a flexible data model, but
not many tools that can take advantage of this. Use cases such as ingesting
files with pre-existing checksums and appending multiple content objects to
single representations require building XIP metadata outside of Preservica's
existing tools.

siplib.py is the central library. The Sip class inherits from
zipfile.ZipFile. On initilisation, this class creates an empty zipfile. Various
methods then write content to the zipfile in a structure that Preservica can
then interpret on ingest. The serialise() method finally writes xml metadata
that has been built up.
