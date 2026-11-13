import os

import psycopg


def test_a_connection_dropped_while_idle_is_replaced(store):
    with store.connect() as conn:
        pid = conn.info.backend_pid
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as killer:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert store.find_user("+910000000000") is None  # the pool notices the dead connection on checkout and opens a new one
