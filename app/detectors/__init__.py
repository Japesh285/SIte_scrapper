from app.detectors.browser import inspect_browser_network
from app.detectors.browser_probe import run_browser_probe
from app.detectors.dom_browser import detect_dom_browser
from app.detectors.dom_infinite_scroll import detect_dom_infinite_scroll
from app.detectors.dom_load_more import detect_dom_load_more
from app.detectors.dynamic_api_detector import detect_dynamic_api
from app.detectors.interactive_dom_detector import detect_interactive_dom
from app.detectors.greenhouse import detect_greenhouse
from app.detectors.simple_api import detect_simple_api
from app.detectors.workday import detect_workday

__all__ = [
    "inspect_browser_network",
    "run_browser_probe",
    "detect_dom_browser",
    "detect_dom_load_more",
    "detect_dom_infinite_scroll",
    "detect_dynamic_api",
    "detect_interactive_dom",
    "detect_workday",
    "detect_greenhouse",
    "detect_simple_api",
]
