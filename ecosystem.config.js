module.exports = {
  apps: [
    {
      name: 'scalp-bot',
      script: 'scripts/scalp_bot.py',
      interpreter: 'python3',
      args: '--interval 180',

      // Restart policy
      autorestart: true,
      max_restarts: 10,
      min_uptime: '30s',
      restart_delay: 5000,

      // Logs
      log_file: 'logs/scalp_bot_pm2.log',
      error_file: 'logs/scalp_bot_error.log',
      out_file: 'logs/scalp_bot_out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true,

      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
    {
      name: 'report-bot',
      script: 'scripts/report_bot.py',
      interpreter: 'python3',
      args: '--interval 3600',

      // Restart policy
      autorestart: true,
      max_restarts: 10,
      min_uptime: '30s',
      restart_delay: 5000,

      // Logs
      log_file: 'logs/report_bot_pm2.log',
      error_file: 'logs/report_bot_error.log',
      out_file: 'logs/report_bot_out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true,

      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
