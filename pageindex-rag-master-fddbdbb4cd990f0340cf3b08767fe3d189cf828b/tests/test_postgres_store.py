from pageindex.postgres_store import PostgresWorkspaceStore


def test_scope_clause_defaults_to_user_scope():
    store = PostgresWorkspaceStore(
        dsn="postgresql://example",
        auto_init_tables=False,
        user_id="system",
    )

    clause, params = store._scope_clause(include_where=True)

    assert clause == "WHERE user_id = %s"
    assert params == ["system"]


def test_scope_clause_allows_cross_user_mode_with_star_user_id():
    store = PostgresWorkspaceStore(
        dsn="postgresql://example",
        auto_init_tables=False,
        user_id="*",
    )

    clause, params = store._scope_clause(include_where=True)

    assert clause == ""
    assert params == []
