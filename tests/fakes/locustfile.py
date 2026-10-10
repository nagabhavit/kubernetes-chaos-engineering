"""Minimal Locust file for tests only (the real one is src/loadgenerator/locustfile.py)."""
from locust import HttpUser, between, task


class WebsiteUser(HttpUser):
    wait_time = between(0.5, 1)

    @task
    def index(self):
        self.client.get("/")
