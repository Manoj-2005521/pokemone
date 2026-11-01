import time


class UserRateLimiter:
    def __init__(
        self,
        capacity: float = 10,
        refill_per_second: float = 1,
        idle_timeout_sec: float = 3600,
        notification_cooldown_sec: float = 3,
    ) -> None:
        if capacity <= 0 or refill_per_second <= 0 or idle_timeout_sec <= 0:
            raise ValueError("Rate limit capacity, refill rate, and idle timeout must be positive.")
        if notification_cooldown_sec < 0:
            raise ValueError("Notification cooldown cannot be negative.")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.idle_timeout_sec = idle_timeout_sec
        self.notification_cooldown_sec = notification_cooldown_sec
        self._buckets: dict[int, tuple[float, float]] = {}
        self._last_notification: dict[int, float] = {}
        self._operations = 0

    def allow(self, user_id: int, now: float | None = None) -> bool:
        if user_id <= 0:
            return True

        current_time = time.monotonic() if now is None else now
        tokens, last_seen = self._buckets.get(
            user_id,
            (self.capacity, current_time),
        )
        elapsed = max(0.0, current_time - last_seen)
        tokens = min(self.capacity, tokens + elapsed * self.refill_per_second)
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        self._buckets[user_id] = (tokens, current_time)

        self._operations += 1
        if self._operations % 256 == 0:
            cutoff = current_time - self.idle_timeout_sec
            inactive = {
                key for key, (_, seen_at) in self._buckets.items()
                if seen_at < cutoff
            }
            for key in inactive:
                self._buckets.pop(key, None)
                self._last_notification.pop(key, None)

        return allowed

    def should_notify(self, user_id: int, now: float | None = None) -> bool:
        current_time = time.monotonic() if now is None else now
        last_notification = self._last_notification.get(user_id)
        if (
            last_notification is not None
            and current_time - last_notification < self.notification_cooldown_sec
        ):
            return False
        self._last_notification[user_id] = current_time
        return True
