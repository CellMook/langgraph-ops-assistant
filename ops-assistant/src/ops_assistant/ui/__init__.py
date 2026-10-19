"""Streamlit UI 包：浏览器端会话界面（只通过 HTTP 调用 FastAPI，不直接访问图/数据库）。

启动方式::

    streamlit run src/ops_assistant/ui/streamlit_app.py

界面需要先启动 API：``uvicorn ops_assistant.api.main:app --port 8000``；
API 地址通过环境变量 ``OPS_API_BASE_URL`` 覆盖。
"""

__all__ = ["streamlit_app"]
