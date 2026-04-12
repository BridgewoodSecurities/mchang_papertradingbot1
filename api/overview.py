from api.index import _load_snapshot, _respond_json


def app(environ, start_response):
    return _respond_json(start_response, 200, _load_snapshot())
