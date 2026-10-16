from auditry.otel_context import otel_extra_installed

# test_otel.py needs the otel extra (Python 3.10+).
collect_ignore: list[str] = [] if otel_extra_installed() else ["test_otel.py"]
