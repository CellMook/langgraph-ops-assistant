"""API 包：FastAPI 应用与请求/响应模型。

注意：本模块不导入 ``main``，避免 ``import ops_assistant.api`` 时连带初始化
FastAPI 应用对象；需要 ASGI 应用时请显式导入 ``ops_assistant.api.main:app``。
"""

__all__ = ["schemas"]
