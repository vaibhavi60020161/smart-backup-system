import os
os.environ["TEAMB_NO_AUTOSTART"] = "1"   # do not create a default store while testing
import os
import pytest
from app.engine import Engine


@pytest.fixture(params=["hash", "avl"])
def engine(tmp_path, request):
    return Engine(str(tmp_path / "store"), index_type=request.param)


@pytest.fixture
def make_file(tmp_path):
    def _make(name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)
    return _make


def req(fid, path, **kw):
    d = {"file_id": fid, "file_path": path, "backup_id": "B1"}
    d.update(kw)
    return d
