"""Finite recovery bursts with cooldown; a nudge never grants send permission."""


class RecoveryBudget:
    LIMIT = 6
    DELAYS = (1, 3, 10, 30, 60)
    COOLDOWN = 60

    def __init__(self):
        self.attempts = 0
        self.next_at = 0
        self.cooldown = False
        self.last_nudge = -float('inf')
        self.last_failure = -float('inf')

    def ready(self, now):
        if now < self.next_at:return False
        if self.cooldown:
            self.attempts = 0
            self.cooldown = False
        return True

    def failure(self, now):
        self.last_failure = now
        self.attempts += 1
        self.cooldown = self.attempts >= self.LIMIT
        delay = self.COOLDOWN if self.cooldown else self.DELAYS[self.attempts-1]
        self.next_at = now + delay

    def defer(self, now):
        self.next_at = max(self.next_at, now+1)

    def nudge(self, now):
        if now-self.last_nudge < 1:return
        self.last_nudge = now
        # Do not reset an exhausted burst or consume more than one new attempt
        # per second when several wake/reconnect notifications arrive together.
        if not self.cooldown:self.next_at = min(self.next_at, max(now,self.last_failure+1))

    def success(self):
        self.attempts = 0
        self.next_at = 0
        self.cooldown = False

    def snapshot(self, now):
        return {'attempts':self.attempts, 'limit':self.LIMIT,
                'cooldown':self.cooldown, 'retry_in_seconds':round(max(0, self.next_at-now), 1)}
