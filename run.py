"""Entry point for running JobHunt directly."""
import importlib.util, sys, os

# Set up a fake package namespace so relative imports work
pkg = type(sys)('jobhunt')
pkg.__path__ = [os.path.dirname(__file__)]
sys.modules['jobhunt'] = pkg

spec = importlib.util.spec_from_file_location(
    'jobhunt.app',
    os.path.join(os.path.dirname(__file__), 'app.py'),
    submodule_search_locations=[os.path.dirname(__file__)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules['jobhunt.app'] = mod
spec.loader.exec_module(mod)

mod.main()
