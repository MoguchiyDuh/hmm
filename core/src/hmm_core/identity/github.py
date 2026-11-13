import httpx
from pydantic import BaseModel, ValidationError

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"


class GithubError(Exception):
    pass


class GithubUser(BaseModel):
    id: int
    login: str
    name: str | None = None
    avatar_url: str | None = None


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read:user",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return str(httpx.URL(AUTHORIZE_URL, params=params))


async def exchange_code(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    verifier: str,
) -> str:
    try:
        response = await http.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise GithubError(f"Token exchange failed: {error}") from error
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise GithubError(f"Token exchange failed: {payload}")
    return token


async def fetch_user(http: httpx.AsyncClient, token: str) -> GithubUser:
    try:
        response = await http.get(
            USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return GithubUser.model_validate_json(response.content)
    except (httpx.HTTPError, ValidationError) as error:
        raise GithubError(f"User lookup failed: {error}") from error
