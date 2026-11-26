from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError


class Problem(Exception):
    def __init__(self, status, code, message, *, uncertain=False, draft=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.uncertain = uncertain
        self.draft = draft


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def post(
    client: httpx.Client,
    label: str,
    path: str,
    payload: BaseModel,
    schema: type[ResponseModel],
    expected_status: int,
) -> ResponseModel:
    name = "ML-сервис" if label == "ml" else "сервис ТОиР"
    try:
        response = client.post(path, json=payload.model_dump(mode="json"))
    except httpx.TimeoutException as error:
        raise Problem(504, f"{label}_timeout", f"Истекло время ожидания ответа: {name}.",
                      uncertain=not isinstance(error, (httpx.ConnectTimeout, httpx.PoolTimeout))) from error
    except httpx.ConnectError as error:
        raise Problem(503, f"{label}_unavailable", f"Нет соединения: {name}.") from error
    except httpx.RequestError as error:
        raise Problem(502, f"{label}_connection_error", f"Соединение прервано: {name}.",
                      uncertain=True) from error

    if response.status_code == 404:
        raise Problem(404, f"{label}_not_found", f"Пара ТС и сессии отсутствует в системе: {name}.")
    if response.status_code in (418, 422):
        raise Problem(422, f"{label}_rejected", f"Сервис отклонил запрос по контракту: {name}.")
    if response.status_code != expected_status:
        raise Problem(502, f"{label}_unexpected_status",
                      f"Неожиданный HTTP {response.status_code}: {name}.", uncertain=True)
    try:
        return schema.model_validate_json(response.content)
    except ValidationError as error:
        raise Problem(502, f"{label}_invalid_response", f"Ответ не соответствует контракту: {name}.",
                      uncertain=True) from error
