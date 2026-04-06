module.exports = {
  apps: [
    {
      name: "exness-forex-bot",
      script: "main.py",
      interpreter: "python3",
      cwd: "./",
      watch: false,
      autorestart: true,
      max_restarts: 15,
      restart_delay: 5000,
      min_uptime: "10s",
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: "."
      },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "logs/bot-error.log",
      out_file: "logs/bot-out.log",
      merge_logs: true,
      max_memory_restart: "500M"
    }
  ]
};
