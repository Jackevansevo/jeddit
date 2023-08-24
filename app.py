import json
from functools import lru_cache
from http import HTTPStatus
from typing import Annotated, Optional
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import httpx
import mistletoe
import redis
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic_settings import BaseSettings, SettingsConfigDict

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

USER_AGENT = "jeddit/0.1 by According_Treat_6399"

templates = Jinja2Templates(directory="templates")


def marked_filter(text):
    return mistletoe.markdown(text)


# TODO
# - Fetch subscribed subreddits + search
# - Implement upvote/downvotes
# - Support different embeded media types


templates.env.filters["marked"] = marked_filter


class Settings(BaseSettings):
    client_id: str
    client_secret: str
    redirect_uri: str
    redis_url: str = "redis://"

    model_config = SettingsConfigDict(env_file=".env")


def generate_access_token(client_id, client_secret):
    resp = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        data=urlencode({"grant_type": "client_credentials", "duration": "permanent"}),
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code == HTTPStatus.OK:
        return resp.json()["access_token"]


@lru_cache()
def get_settings():
    return Settings()


def get_redis_url(settings: Annotated[Settings, Depends(get_settings)]) -> str:
    return settings.redis_url


@lru_cache()
def get_pool(redis_url: Annotated[Settings, Depends(get_redis_url)]):
    return redis.ConnectionPool.from_url(redis_url)


def get_cache(
    pool: Annotated[redis.ConnectionPool, Depends(get_pool)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> redis.Redis:
    return redis.Redis(connection_pool=pool)


def get_user(
    user_token: Annotated[str | None, Cookie()] = None, cache=Depends(get_cache)
):
    if user_token is not None:
        cached_info = cache.get(user_token)
        if cached_info is not None:
            return json.loads(cached_info)
        else:
            cache.delete(user_token)
    return {}


def get_access_token(
    user_info: Annotated[dict, Depends(get_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    cache=Depends(get_cache),
):
    user_token = user_info.get("access_token")
    if user_token is not None:
        return user_token

    token = cache.get("access_token")
    if token is not None:
        return token.decode()
    token = generate_access_token(settings.client_id, settings.client_secret)
    if token is not None:
        cache.set("access_token", token)
    return token


def reddit_fetch(cache: redis.Redis, target_url: str, bearer_token: str, settings):
    data = cache.get(target_url)
    if data is not None:
        print("cache hit for", target_url)
        return json.loads(data), {}

    print("fetching", target_url)
    resp = httpx.get(
        target_url,
        headers={
            "Authorization": f"bearer {bearer_token}",
            "User-Agent": USER_AGENT,
        },
        follow_redirects=True,
    )
    if resp.status_code != HTTPStatus.OK:
        if resp.status_code == HTTPStatus.UNAUTHORIZED:
            token = generate_access_token(settings.client_id, settings.client_secret)
            if token is not None:
                cache.set("access_token", token)
            return reddit_fetch(cache, target_url, token, settings)
        raise HTTPException(status_code=resp.status_code)
    used = resp.headers.get("x-ratelimit-used")
    remaining = resp.headers.get("x-ratelimit-remaining")
    reset = resp.headers.get("x-ratelimit-reset")
    print(
        f"rate limit used: {used}" f"rate limit remaining: {remaining}",
        f"rate limit reset: {reset}",
    )
    data = resp.json()
    cache.set(target_url, resp.content, ex=60 * 5)
    return data, {"used": used, "remaining": remaining, "reset": reset}


@app.get("/logout")
def logout(
    response: Response,
    user_token: Annotated[str | None, Cookie()] = None,
    cache=Depends(get_cache),
):
    response = RedirectResponse("/")
    response.delete_cookie(key="user_token")
    cache.delete(user_token)
    return response


@app.get("/login")
def login(response: Response, settings: Annotated[Settings, Depends(get_settings)]):
    user_token = uuid4()
    redirect_url = (
        urlparse("https://www.reddit.com")
        ._replace(
            path="api/v1/authorize",
            query=urlencode(
                {
                    "client_id": settings.client_id,
                    "response_type": "code",
                    "state": user_token,
                    "duration": "permanent",
                    "redirect_uri": settings.redirect_uri,
                    "scope": "identity mysubreddits read",
                }
            ),
        )
        .geturl()
    )

    response = RedirectResponse(redirect_url)
    response.set_cookie(key="user_token", value=str(user_token))
    return response


@app.get("/auth")
async def auth(
    code: str,
    state: str,
    user_token: Annotated[str | None, Cookie()],
    settings: Annotated[Settings, Depends(get_settings)],
    cache=Depends(get_cache),
):
    if user_token != state:
        raise HTTPException(status=HTTPStatus.UNAUTHORIZED)

    grant_type = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
        }
    )
    access_token_resp = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        data=grant_type,
        auth=(settings.client_id, settings.client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if access_token_resp.status_code == HTTPStatus.OK:
        access_token_data = access_token_resp.json()
        bearer_token = access_token_data["access_token"]
        me_resp = httpx.get(
            "https://oauth.reddit.com/api/v1/me",
            headers={
                "Authorization": f"bearer {bearer_token}",
                "User-Agent": USER_AGENT,
            },
            follow_redirects=True,
        )
        cache.set(user_token, json.dumps(access_token_resp.json() | me_resp.json()))
    else:
        raise HTTPException(status_code=access_token_resp.status_code)

    return RedirectResponse("/")


@app.get("/favicon.ico")
def favicon():
    return FileResponse("static/favicon.ico")


@app.get("/r/{subreddit}/comments/{id}/{userId}/")
def comments(
    request: Request,
    user_info: Annotated[dict, Depends(get_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    access_token: Annotated[str, Depends(get_access_token)],
    cache=Depends(get_cache),
    subreddit: Optional[str] = None,
    category: Optional[str] = None,
):
    target_url = (
        urlparse("https://oauth.reddit.com")
        ._replace(path=request.url.path, query=urlencode(request.query_params))
        .geturl()
    )
    data, api_stats = reddit_fetch(cache, target_url, access_token, settings)
    stats_key = user_info["name"] + ".api_stats " if user_info else "api_stats"
    if api_stats:
        cache.set(stats_key, json.dumps(api_stats))
    else:
        cached_api_stats = cache.get(stats_key)
        if cached_api_stats is not None:
            api_stats = json.loads(cached_api_stats)

    post, comments = data
    return templates.TemplateResponse(
        "post.html",
        {
            "user": user_info,
            "api_stats": api_stats,
            "request": request,
            "post": post.get("data").get("children")[0],
            "comments": comments.get("data").get("children"),
            "subreddit": subreddit,
            "category": category,
        },
    )


@app.get("/")
@app.get("/{category}")
@app.get("/r/{subreddit}/")
@app.get("/r/{subreddit}/{category}")
def subreddit(
    request: Request,
    user_info: Annotated[dict, Depends(get_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    access_token: Annotated[str, Depends(get_access_token)],
    cache=Depends(get_cache),
    subreddit: Optional[str] = None,
    category: Optional[str] = None,
):
    if request.url.path == "/" and not user_info:
        path = "/r/popular"
    else:
        path = request.url.path

    target_url = (
        urlparse("https://oauth.reddit.com")
        ._replace(path=path, query=urlencode(request.query_params))
        .geturl()
    )

    data, api_stats = reddit_fetch(cache, target_url, access_token, settings)
    stats_key = user_info["name"] + ".api_stats " if user_info else "api_stats"
    if api_stats:
        cache.set(stats_key, json.dumps(api_stats))
    else:
        cached_api_stats = cache.get(stats_key)
        if cached_api_stats is not None:
            api_stats = json.loads(cached_api_stats)

    return templates.TemplateResponse(
        "subreddit.html",
        {
            "user": user_info,
            "api_stats": api_stats,
            "request": request,
            "api_resp": data,
            "subreddit": subreddit,
            "category": category,
        },
    )
