from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class Result(BaseModel, Generic[T]):
    """统一响应体，严格对齐 Java 侧 `com.lengbot.common.Result`。

    前端拦截器按 `code === 200` 判断成功，字段名一个都不能改。
    """

    code: int = 200
    message: str = "success"
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None) -> "Result[T]":
        return cls(code=200, message="success", data=data)

    @classmethod
    def fail(cls, message: str, code: int = 500) -> "Result[T]":
        return cls(code=code, message=message, data=None)


class PageResult(BaseModel, Generic[T]):
    """分页响应，对齐 MyBatis-Plus 的 `Page<T>` 序列化结果。

    MP 实际还会输出 orders / optimizeCountSql 等字段，但前端只用这四个，
    这里保持最小契约；后续若发现前端读了别的字段再补。
    """

    model_config = ConfigDict(populate_by_name=True)

    records: list[T] = Field(default_factory=list)
    total: int = 0
    size: int = 20
    current: int = 1
    pages: int = 0

    @classmethod
    def of(cls, records: list[T], total: int, current: int, size: int) -> "PageResult[T]":
        pages = (total + size - 1) // size if size > 0 else 0
        return cls(records=records, total=total, size=size, current=current, pages=pages)


class CamelModel(BaseModel):
    """camelCase 输出的基类。

    Java 的 Jackson 默认输出 camelCase（agentId / messageCount），
    Python 习惯 snake_case。这里用 alias_generator 自动转换，
    保证前端拿到的字段名和现在完全一致。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
