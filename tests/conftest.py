import os

# Must be set before odoo_database_tool / Qt is imported
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _isolated_config(tmp_path_factory):
    base = tmp_path_factory.mktemp("cfg")
    os.environ["ODT_SETTINGS"] = str(base / "settings.ini")
    os.environ["ODT_CONFIG_DIR"] = str(base / "config")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Spin a QEventLoop (polling every 10 ms) until predicate() is true; False on timeout."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    limit = QTimer()
    limit.setSingleShot(True)
    limit.timeout.connect(loop.quit)
    if predicate():
        return True
    poll.start()
    limit.start(int(timeout * 1000))
    loop.exec()
    poll.stop()
    limit.stop()
    return bool(predicate())


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ODT_TEST_PG_URL"):
        return
    skip = pytest.mark.skip(reason="needs ODT_TEST_PG_URL (CI only)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
