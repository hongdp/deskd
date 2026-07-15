"""deskd web console.

Optional: importing this package pulls in fastapi/pydantic, which the engine
core does not require. Install with the `web` extra, then:

    uvicorn --factory deskd.web.app:create_app

or, from a host that configures the engine itself:

    from deskd.web.app import create_app
    app = create_app(my_engine_config)
"""

from .app import create_app

__all__ = ["create_app"]
