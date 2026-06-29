# Three ways to rate-limit an API

Every public API has to decide what happens when a client sends too many requests. The answer shapes how the service behaves under load and how much state you keep per caller. This note compares the three algorithms you will actually reach for and shows one of them step by step, with the code to start from.

## The shortlist

The choice usually comes down to a token bucket, a leaky bucket, or a fixed window counter. They differ most in how they treat a sudden burst and in how much memory each caller costs you.

| Property | Token bucket | Leaky bucket | Fixed window |
|---|---|---|---|
| Allows short bursts | [+] **yes, up to bucket size** | [-] no, paces output | [~] only near a boundary |
| Memory per client | [~] two numbers | [~] a queue or counter | [+] **one counter** |
| Smooths output rate | [-] no | [+] **yes** | [-] no |
| Boundary burst risk | [+] none | [+] none | [-] 2x at the window edge |
| Typical use | general API limits | traffic shaping | simple quotas |

Token bucket wins for most web APIs because it absorbs the natural burstiness of real traffic while still capping the long-run rate. Leaky bucket is the right tool when a downstream system needs a steady feed. Fixed window is the cheapest to run and the easiest to reason about, which is why so many quota systems still use it.

## How a token bucket fills and drains

A token bucket holds up to N tokens. Tokens refill at a steady rate. Each request spends one token, and a request that finds the bucket empty is rejected or queued. The walkthrough below uses a bucket of size 4 refilling at one token per second.

```stepper title="Token bucket, size 4, refill 1/s"
Start: the bucket holds 4 tokens and the client is idle.
---
A burst of 3 requests arrives at once. Each spends a token, leaving 1. All three are allowed, because the bucket had saved that capacity while the client was quiet.
---
A 4th request arrives immediately. One token remains, so it is spent and the bucket is now empty.
---
A 5th request arrives in the same instant. The bucket is empty, so this request is rejected with HTTP 429.
---
One second passes with no traffic. The refill adds a token, so the bucket holds 1 again and the next request will succeed.
```

The saved capacity is the whole point. A client that has been quiet can spend a burst immediately, and a client that hammers the endpoint settles into the refill rate. That matches how people actually use an API.

```embed
<figure class="diagram" style="margin:1.5rem 0;text-align:center">
  <svg viewBox="0 0 440 220" role="img" aria-label="Token bucket schematic" xmlns="http://www.w3.org/2000/svg" style="max-width:440px;width:100%;height:auto;font-family:inherit">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#0072B2"/>
      </marker>
      <marker id="arrowOut" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#D55E00"/>
      </marker>
    </defs>
    <text x="220" y="18" text-anchor="middle" font-size="13" fill="#0072B2">refill 1 token / second</text>
    <line x1="220" y1="24" x2="220" y2="58" stroke="#0072B2" stroke-width="2" marker-end="url(#arrow)"/>
    <rect x="160" y="60" width="120" height="108" rx="8" fill="#F7F7F7" stroke="#000" stroke-width="2"/>
    <text x="220" y="80" text-anchor="middle" font-size="12" fill="#555">capacity 4</text>
    <circle cx="190" cy="146" r="11" fill="#009E73"/>
    <circle cx="220" cy="146" r="11" fill="#009E73"/>
    <circle cx="190" cy="118" r="11" fill="none" stroke="#bbb" stroke-dasharray="3 3"/>
    <circle cx="220" cy="118" r="11" fill="none" stroke="#bbb" stroke-dasharray="3 3"/>
    <line x1="220" y1="170" x2="220" y2="204" stroke="#D55E00" stroke-width="2" marker-end="url(#arrowOut)"/>
    <text x="300" y="192" font-size="13" fill="#D55E00">a request spends 1</text>
    <text x="300" y="120" font-size="12" fill="#555">empty bucket</text>
    <text x="300" y="136" font-size="12" fill="#555">returns HTTP 429</text>
  </svg>
  <figcaption style="font-size:0.85rem;color:#555">A token bucket: steady refill, bursts spend the saved tokens, an empty bucket returns 429.</figcaption>
</figure>
```

## The core check

The implementation is small. Store the token count and the last refill time per client, then top up the count lazily on each request.

```python
import time

class TokenBucket:
    def __init__(self, capacity, refill_per_sec):
        self.capacity = capacity
        self.refill = refill_per_sec
        self.tokens = capacity
        self.updated = time.monotonic()

    def allow(self):
        now = time.monotonic()
        elapsed = now - self.updated
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False
```

Lazy refill keeps the cost at two numbers per client and one subtraction per request. No background job sweeps the buckets, so the limiter scales with traffic and not with the number of idle clients.

## Where each one bites

Fixed window has a sharp edge. A client can send a full window of requests just before the boundary and a second full window just after, so a "100 per minute" limit can permit 200 in the two seconds straddling the reset. A sliding window log or sliding window counter closes that gap at the cost of more state. Token bucket and leaky bucket avoid the boundary problem by construction, which is the main reason both show up in production gateways.
