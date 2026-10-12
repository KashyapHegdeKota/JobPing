"""Redis server-time algorithms; all decisions and TTL writes are atomic."""

TOKEN_BUCKET = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local rate, burst = tonumber(ARGV[1]), tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(state[1]) or burst
local at = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - at) * rate)
local allowed, retry = 0, 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'at', now)
redis.call('EXPIRE', KEYS[1], math.max(1, math.ceil(burst / rate)))
return {allowed, retry}
"""

LEASE_ACQUIRE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local ttl, ip_limit, global_limit = tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
for _, key in ipairs(KEYS) do redis.call('ZREMRANGEBYSCORE', key, '-inf', now) end
if (ip_limit > 0 and redis.call('ZCARD', KEYS[1]) >= ip_limit)
  or (global_limit > 0 and redis.call('ZCARD', KEYS[2]) >= global_limit) then
  return 0
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now + ttl, ARGV[1])
  redis.call('EXPIRE', key, math.ceil(ttl * 2))
end
return 1
"""

LEASE_RENEW = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
for _, key in ipairs(KEYS) do
  local expiry = tonumber(redis.call('ZSCORE', key, ARGV[1]))
  if not expiry or expiry <= now then return 0 end
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now + tonumber(ARGV[2]), ARGV[1])
  redis.call('EXPIRE', key, math.ceil(tonumber(ARGV[2]) * 2))
end
return 1
"""

LEASE_RELEASE = """
for _, key in ipairs(KEYS) do redis.call('ZREM', key, ARGV[1]) end
return 1
"""
