import os
import tempfile

# Tests use a throwaway database and never call real services.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ.pop("DATA_GOV_IN_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
