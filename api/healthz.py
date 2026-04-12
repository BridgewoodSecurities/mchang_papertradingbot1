from api.index import _deployment_target, _respond_json


def app(environ, start_response):
    return _respond_json(start_response, 200, {"ok": True, "target": _deployment_target()})
