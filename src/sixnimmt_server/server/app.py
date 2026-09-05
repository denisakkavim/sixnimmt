"""The FastAPI application: wiring, and rendering every refusal as §9.4's shape."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sixnimmt_server.server.auth import TokenRegistry, mint_token
from sixnimmt_server.server.errors import ApiError, ApiErrorCode
from sixnimmt_server.server.routes import bearer_token, router
from sixnimmt_server.server.schemas import ErrorBody, ErrorResponse
from sixnimmt_server.server.store import MatchStore


def _error_response(error: ApiError) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=error.code.value,
            message=error.message,
            legal_actions=list(error.legal_actions),
            view_version=error.view_version,
        )
    )
    return JSONResponse(status_code=error.status, content=body.model_dump(mode="json"))


def create_app(admin_token: str | None = None) -> FastAPI:
    """Build the server. The admin token is server-wide because match creation
    needs authority before any match exists."""
    app = FastAPI(title="6 nimmt! game server", version="1.0.0")
    tokens = TokenRegistry(admin_token or mint_token())
    app.state.tokens = tokens
    app.state.store = MatchStore(tokens)
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(error)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # A hostile or confused agent will send malformed bodies constantly.
        # They get the same error shape as everything else, never a stack trace.
        refusal = ApiError(
            ApiErrorCode.MALFORMED_REQUEST,
            f"the request body could not be read: {len(error.errors())} problems",
        )
        return _error_response(_with_caller_view(request, refusal))

    return app


def _with_caller_view(request: Request, error: ApiError) -> ApiError:
    """Add the caller's own legal actions and cursor when the token identifies them.

    A malformed body still deserves the §9.4 hint about what it could have sent.
    Anything the caller cannot be identified for is left out rather than guessed.
    """
    match_id = request.path_params.get("match_id")
    if not isinstance(match_id, str):
        return error
    try:
        record, viewer = request.app.state.store.authorise(bearer_token(request), match_id)
    except ApiError:
        return error
    view = record.view_for(viewer)
    error.legal_actions = view.legal_actions
    error.view_version = view.view_version
    return error
