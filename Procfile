web: bin/start-nginx gunicorn -c config/gunicorn.conf config.wsgi:application
worker: celery worker --app=squarelet.taskapp --loglevel=info
beat: celery beat --app=squarelet.taskapp --loglevel=info
