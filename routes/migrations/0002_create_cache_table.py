"""Creates the Django database cache table via a migration.

This mirrors ``manage.py createcachetable django_cache`` so the table exists in
the main database AND in the pytest test database without a manual step.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("routes", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE IF NOT EXISTS django_cache (
                cache_key varchar(255) PRIMARY KEY,
                value text NOT NULL,
                expires timestamptz NOT NULL
            );
            CREATE INDEX IF NOT EXISTS django_cache_expires_idx
                ON django_cache (expires);
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS django_cache_expires_idx;
            DROP TABLE IF EXISTS django_cache;
            """,
        ),
    ]