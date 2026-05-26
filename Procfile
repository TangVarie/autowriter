web: streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true
# R-018: 后台 job worker。Render / Railway / Heroku 类平台会作为独立 service 跑;
# 需在该 service 配 SUPABASE_SERVICE_ROLE_KEY。Streamlit Cloud 忽略 Procfile,
# 那种部署需另起常驻主机(systemd/supervisor)跑 `python worker.py`。
worker: python worker.py
