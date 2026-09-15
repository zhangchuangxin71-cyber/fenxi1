from __future__ import annotations

import pytest

from app.persistence.bootstrap import application_database, create_database


def test_application_database_parses_percent_encoded_credentials() -> None:
    target = application_database(
        "postgresql://wechat_article_agent:p%40ss%2Fword@postgres:5432/wechat_article_agent"
    )

    assert target.name == "wechat_article_agent"
    assert target.user == "wechat_article_agent"
    assert target.password == "p@ss/word"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:password@postgres:5432/",
        "postgresql://user@postgres:5432/database",
    ],
)
def test_application_database_requires_dedicated_credentials(database_url: str) -> None:
    with pytest.raises(ValueError):
        application_database(database_url)


async def test_database_bootstrap_rejects_same_admin_and_application_role() -> None:
    with pytest.raises(ValueError, match="different database roles"):
        await create_database(
            admin_url="postgresql://wechat:admin@postgres:5432/postgres",
            database_url="postgresql://wechat:application@postgres:5432/wechat",
        )
