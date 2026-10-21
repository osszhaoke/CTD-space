import os


def database_options():
    return {
        "host": os.environ.get("POSTGRES_HOST", "db"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "database": os.environ.get("POSTGRES_DB", "postgres"),
        "user": os.environ.get("POSTGRES_USER", "postgres"),
        "password": os.environ.get("POSTGRES_PASSWORD", "docker"),
        "min_size": 1,
        "max_size": 5,
        "command_timeout": 15,
    }
