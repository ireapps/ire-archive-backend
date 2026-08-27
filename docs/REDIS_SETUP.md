# Redis Setup Guide for IRE Resources Semantic Search

This guide explains how to set up Redis for session storage in both local development and production (Fly.io) environments.

## Overview

Redis is used to store server-side session data for the MemberSuite SSO authentication system. The AuthToken is stored in Redis and never sent to the browser, which enhances security.

## Local Development Setup

### Prerequisites

- Docker and Docker Compose installed
- Make targets available (`make dev-*`) or run scripts via `uv run python -m scripts.dev_tasks ...`

### Quick Start

1. **Start all services (including Redis):**

```bash
make dev-start
```

This command will:

- Start Qdrant (vector database) in Docker
- Start Redis (session store) in Docker
- Start the FastAPI backend with `REDIS_URL=redis://localhost:6379`

2. **Verify Redis is running:**

```bash
# Check service status
make dev-status

# Test Redis connection
docker exec -it ire-redis redis-cli ping
# Should return: PONG
```

3. **View Redis data:**

```bash
# Connect to Redis CLI
docker exec -it ire-redis redis-cli

# List all keys
127.0.0.1:6379> KEYS *

# View a session (replace with actual session ID)
127.0.0.1:6379> GET session:abc123...

# Get all sessions count
127.0.0.1:6379> KEYS session:* | wc -l
```

4. **Stop all services:**

```bash
make dev-stop
```

### Manual Docker Setup (Alternative)

If you prefer to manage Docker services separately:

```bash
# Start only Docker services
docker-compose up -d

# Stop Docker services
docker-compose down

# View Redis logs
docker logs ire-redis

# Remove all data (fresh start)
docker-compose down -v
```

### Configuration

The local Redis configuration is in `docker/docker-compose.yml`:

- **Port**: 6379 (standard Redis port)
- **Data persistence**: Enabled with AOF (Append-Only File)
- **Fsync policy**: everysec (good balance of performance and durability)
- **Network**: Shared Docker network with Qdrant
- **Health checks**: Redis CLI ping every 10 seconds

## Production Setup (Fly.io)

### Option 1: Fly.io Redis (Recommended)

Fly.io provides managed Redis instances with automatic backups and high availability.

#### Step 1: Create Fly Redis Instance

```bash
# Create Redis instance in same region as your app
fly redis create my-redis --region iad

# This will output a connection string like:
# redis://default:YOUR_PASSWORD@your-redis-host.fly.dev:6379
```

#### Step 2: Connect Your App

The Redis instance is automatically connected via Fly.io's private network. Set the secret:

```bash
# Set Redis URL secret
fly secrets set REDIS_URL="redis://default:YOUR_PASSWORD@your-redis-host.fly.dev:6379"

# Verify secrets are set
fly secrets list
```

#### Step 3: Deploy

```bash
# Deploy with new configuration
fly deploy

# Verify Redis connectivity
fly ssh console
$ fly ssh console
Connecting to fdaa:x:xxxx:xxx:xx:xxxx:xxxx:x... complete
root@your-machine-id:/app# python3 -c "
import redis
import os
r = redis.from_url(os.environ['REDIS_URL'])
print(r.ping())
"
True
```

### Option 2: Upstash Redis (Alternative)

Upstash provides serverless Redis with a generous free tier.

#### Step 1: Create Upstash Database

1. Sign up at [upstash.com](https://upstash.com)
2. Create a new Redis database
3. Select a region close to your Fly.io app (e.g., us-east-1)
4. Enable TLS
5. Copy the connection URL (format: `redis://default:YOUR_PASSWORD@host:6379`)

#### Step 2: Configure Fly.io

```bash
# Set Upstash URL as a secret
fly secrets set REDIS_URL="rediss://default:YOUR_PASSWORD@host.upstash.io:6379"

# Note: Use 'rediss://' (with double 's') for TLS connections

# Deploy
fly deploy
```

### Monitoring Redis in Production

#### Check Redis Status

```bash
# Connect to Fly.io app
fly ssh console

# Check Redis connectivity
redis-cli -u $REDIS_URL ping

# View Redis info
redis-cli -u $REDIS_URL INFO

# Check memory usage
redis-cli -u $REDIS_URL INFO memory

# Count active sessions
redis-cli -u $REDIS_URL --scan --pattern "session:*" | wc -l
```

#### View Session Data

```bash
# Connect to Redis
fly redis connect

# List all session keys
KEYS session:*

# View a specific session (sessions are JSON)
GET session:abc123xyz...

# Check session TTL (time to live)
TTL session:abc123xyz...
```

### Redis Configuration in Production

Set these environment variables for fine-tuning:

```bash
# Session TTL (1 hour = 3600 seconds)
fly secrets set SESSION_TTL_SECONDS=3600

# Session cookie name
fly secrets set SESSION_COOKIE_NAME=ire_session

# Session signing secret (generate with: openssl rand -hex 32)
fly secrets set SESSION_SECRET="your-64-character-random-hex-string"
```

## Troubleshooting

### Local Development Issues

**Problem**: Redis won't start

```bash
# Check if port 6379 is already in use
lsof -i :6379

# Kill process on port 6379
kill -9 $(lsof -t -i:6379)

# Remove old containers and volumes
docker-compose down -v
docker-compose up -d
```

**Problem**: API can't connect to Redis

```bash
# Check Redis is running
docker ps | grep redis

# Check logs
docker logs ire-redis

# Test connection
docker exec -it ire-redis redis-cli ping

# Verify REDIS_URL environment variable
echo $REDIS_URL  # Should be: redis://localhost:6379
```

### Production Issues

**Problem**: Sessions not persisting

```bash
# Check Redis is running
fly redis status my-redis

# Check Redis connectivity from app
fly ssh console
$ redis-cli -u $REDIS_URL ping

# Check app logs for Redis errors
fly logs --app your-fly-app
```

**Problem**: High memory usage

```bash
# Check Redis memory stats
fly redis connect
> INFO memory
> CONFIG GET maxmemory
> CONFIG GET maxmemory-policy

# Clear all sessions (emergency only)
> FLUSHDB
```

**Problem**: Connection timeout

```bash
# Check if Redis is in same region
fly redis status my-redis

# Check app region
fly status

# If different regions, consider moving Redis or adding read replica
fly redis create my-redis-replica --region ord --primary my-redis
```

## Redis Data Structure

Sessions are stored with this structure:

```
Key: session:{session_id}
Value: JSON string containing:
{
  "session_id": "abc123...",
  "user_id": "user-guid",
  "email": "user@example.com",
  "first_name": "Jane",
  "last_name": "Doe",
  "full_name": "Jane Doe",
  "is_active_member": true,
  "membership_id": "membership-guid",
  "created_at": 1234567890.0,
  "expires_at": 1234571490.0,
  "auth_token": "encrypted-membersuite-token"
}

TTL: 3600 seconds (automatically deleted when expired)
```

## Security Notes

1. **AuthToken Security**: The MemberSuite AuthToken is stored in Redis and never sent to the browser. Only a signed session ID is sent as an HttpOnly cookie.

2. **Redis Network**:
   - Local: Redis is isolated in Docker network
   - Fly.io: Redis is on private 6PN network (not exposed to internet)

3. **Session Signing**: Session IDs are signed using `itsdangerous` with `SESSION_SECRET` to prevent tampering.

4. **Data Persistence**:
   - Local: AOF enabled (data survives container restarts)
   - Fly.io: Automatic backups every 24 hours

5. **Encryption in Transit**:
   - Local: No TLS (localhost only)
   - Fly.io: TLS enabled by default on Fly Redis
   - Upstash: Use `rediss://` URL for TLS

## Backup and Recovery

### Local Development

```bash
# Backup Redis data
docker exec ire-redis redis-cli SAVE
docker cp ire-redis:/data/dump.rdb ./redis-backup.rdb

# Restore from backup
docker cp ./redis-backup.rdb ire-redis:/data/dump.rdb
docker restart ire-redis
```

### Production (Fly.io)

Fly Redis includes automatic backups:

```bash
# View backup info
fly redis status my-redis

# Point-in-time recovery (contact Fly.io support)
# Backups are retained for 7 days
```

## Performance Tuning

### Recommended Redis Settings

For production, configure these Redis settings via Fly.io dashboard or CLI:

```redis
# Eviction policy (when memory is full)
maxmemory-policy allkeys-lru

# Max memory (adjust based on needs)
maxmemory 512mb

# Persistence (balance durability vs performance)
appendfsync everysec  # Default, good balance

# For high-traffic production:
appendfsync no  # Faster, less durable
```

### Monitoring Metrics

Key metrics to watch:

- **used_memory**: Should stay below maxmemory
- **connected_clients**: Number of active connections
- **evicted_keys**: Keys removed due to memory pressure
- **expired_keys**: Sessions that expired naturally
- **keyspace_hits/misses**: Cache hit ratio

```bash
# Get current metrics
redis-cli -u $REDIS_URL INFO stats
```

## Cost Estimates

### Local Development

- **Cost**: Free (uses Docker)
- **Memory**: ~50MB for Redis container
- **Disk**: ~10MB for session data

### Fly.io Redis

- **Free tier**: Not available
- **Starting**: ~$1.94/month for 256MB
- **Recommended**: ~$7/month for 1GB with backups

### Upstash

- **Free tier**: 10,000 commands/day, 256MB
- **Pay-as-you-go**: $0.20 per 100K commands
- **Good for**: Low to medium traffic sites

## Integration with MemberSuite SSO

Redis is initialized in the FastAPI app lifespan:

```python
# app/dependencies.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... other initialization ...

    # Initialize Redis
    auth_settings = get_auth_settings()
    if auth_settings.is_configured:
        app.state.redis = redis.from_url(
            auth_settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )

        # Create session manager
        app.state.session_manager = SessionManager(app.state.redis, auth_settings)

    yield

    # Cleanup
    if hasattr(app.state, "redis"):
        await app.state.redis.close()
```

For full SSO integration details, see:

- `docs/MEMBERSUITE_SSO_INTEGRATION.md` - Complete SSO implementation plan
- `app/auth/session.py` - Session management code
- `app/auth/config.py` - Configuration settings

## Next Steps

1. **For Local Development**: Run `make dev-start` and you're ready to develop!

2. **For Production Setup**:
   - Create Fly Redis: `fly redis create my-redis`
   - Set secrets: `fly secrets set REDIS_URL=...`
   - Deploy: `fly deploy`

3. **For SSO Integration**: Follow the MemberSuite SSO implementation plan in Phase 2 (Session Management).
