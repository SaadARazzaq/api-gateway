#!/usr/bin/env python3
"""
API Gateway with Advanced Rate Limiting
A production-ready API gateway with multiple rate limiting strategies.
"""

import asyncio
import time
import hashlib
import json
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from enum import Enum

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import yaml
from pydantic import BaseModel


class RateLimitStrategy(str, Enum):
    TOKEN_BUCKET = "token_bucket"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"


class RateLimitConfig(BaseModel):
    requests: int = 100
    window: int = 60  # seconds
    burst: int = 10
    strategy: RateLimitStrategy = RateLimitStrategy.TOKEN_BUCKET


class RouteConfig(BaseModel):
    path: str
    target: str
    methods: List[str] = ["GET", "POST", "PUT", "DELETE"]
    strip_prefix: bool = True
    rate_limit: Optional[RateLimitConfig] = None
    timeout: int = 30
    cache_ttl: int = 0  # 0 means no caching


class GatewayConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    cors_origins: List[str] = ["*"]
    rate_limiting: RateLimitConfig = RateLimitConfig()
    routes: List[RouteConfig] = []
    redis_url: str = "redis://localhost:6379"


class ClientIdentifier:
    @staticmethod
    def identify_client(request: Request) -> str:
        """Identify client based on IP, API key, or JWT token."""
        # Try API key first
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"apikey:{api_key}"
        
        # Try JWT token
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove "Bearer "
            return f"token:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
        
        # Fall back to IP address
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0]
        else:
            client_ip = request.client.host
            
        return f"ip:{client_ip}"


class RateLimiter:
    def __init__(self, redis_url: str, default_config: RateLimitConfig):
        self.redis = redis.from_url(redis_url)
        self.default_config = default_config
        
    async def is_rate_limited(
        self, 
        client_id: str, 
        route_key: str, 
        config: Optional[RateLimitConfig] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """Check if request should be rate limited."""
        if config is None:
            config = self.default_config
            
        key = f"rate_limit:{client_id}:{route_key}"
        
        if config.strategy == RateLimitStrategy.TOKEN_BUCKET:
            return await self._token_bucket_check(key, config)
        elif config.strategy == RateLimitStrategy.FIXED_WINDOW:
            return await self._fixed_window_check(key, config)
        elif config.strategy == RateLimitStrategy.SLIDING_WINDOW:
            return await self._sliding_window_check(key, config)
        else:
            return await self._token_bucket_check(key, config)
            
    async def _token_bucket_check(self, key: str, config: RateLimitConfig) -> Tuple[bool, Dict[str, Any]]:
        """Token bucket rate limiting algorithm."""
        now = time.time()
        bucket_key = f"{key}:bucket"
        last_update_key = f"{key}:last_update"
        
        async with self.redis.pipeline() as pipe:
            await pipe.watch(bucket_key, last_update_key)
            
            try:
                # Get current token count and last update time
                current_tokens, last_update = await pipe.mget(bucket_key, last_update_key)
                current_tokens = float(current_tokens or config.burst)
                last_update = float(last_update or now)
                
                # Calculate tokens to add based on time elapsed
                time_elapsed = now - last_update
                tokens_to_add = (time_elapsed * config.requests) / config.window
                new_tokens = min(current_tokens + tokens_to_add, config.burst)
                
                # Check if request can be processed
                if new_tokens >= 1:
                    new_tokens -= 1
                    is_limited = False
                else:
                    is_limited = True
                
                # Update bucket
                await pipe.multi()
                await pipe.set(bucket_key, new_tokens, ex=config.window * 2)
                await pipe.set(last_update_key, now, ex=config.window * 2)
                await pipe.execute()
                
                remaining = int(new_tokens)
                reset_time = int(now + config.window)
                
            except redis.WatchError:
                # Race condition occurred, assume not limited
                is_limited = False
                remaining = config.burst - 1
                reset_time = int(now + config.window)
                
        return is_limited, {
            "limit": config.requests,
            "remaining": remaining,
            "reset": reset_time,
            "window": config.window
        }
        
    async def _fixed_window_check(self, key: str, config: RateLimitConfig) -> Tuple[bool, Dict[str, Any]]:
        """Fixed window rate limiting algorithm."""
        now = int(time.time())
        window_key = f"{key}:{now // config.window}"
        
        async with self.redis.pipeline() as pipe:
            await pipe.incr(window_key)
            await pipe.expire(window_key, config.window)
            current_count = await pipe.execute()
            
        current_count = current_count[0]
        is_limited = current_count > config.requests
        
        return is_limited, {
            "limit": config.requests,
            "remaining": max(0, config.requests - current_count),
            "reset": (now // config.window + 1) * config.window,
            "window": config.window
        }
        
    async def _sliding_window_check(self, key: str, config: RateLimitConfig) -> Tuple[bool, Dict[str, Any]]:
        """Sliding window rate limiting algorithm."""
        now = time.time()
        window_start = now - config.window
        
        # Remove old entries
        await self.redis.zremrangebyscore(key, 0, window_start)
        
        # Count requests in current window
        current_count = await self.redis.zcard(key)
        
        if current_count >= config.requests:
            is_limited = True
        else:
            is_limited = False
            # Add current request
            await self.redis.zadd(key, {str(now): now})
            await self.redis.expire(key, config.window)
            
        # Get oldest request to calculate reset time
        oldest = await self.redis.zrange(key, 0, 0, withscores=True)
        reset_time = int(oldest[0][1] + config.window) if oldest else int(now + config.window)
        
        return is_limited, {
            "limit": config.requests,
            "remaining": max(0, config.requests - current_count),
            "reset": reset_time,
            "window": config.window
        }
        
    async def get_metrics(self) -> Dict[str, Any]:
        """Get rate limiting metrics."""
        try:
            info = await self.redis.info()
            return {
                "redis_connected": True,
                "redis_used_memory": info.get("used_memory", 0),
                "redis_connected_clients": info.get("connected_clients", 0)
            }
        except:
            return {"redis_connected": False}


class ResponseCache:
    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)
        
    async def get(self, key: str) -> Optional[bytes]:
        """Get cached response."""
        return await self.redis.get(key)
        
    async def set(self, key: str, data: bytes, ttl: int):
        """Set cached response."""
        await self.redis.setex(key, ttl, data)
        
    def generate_key(self, request: Request) -> str:
        """Generate cache key from request."""
        key_data = {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.query_params),
            "headers": {
                k: v for k, v in request.headers.items() 
                if k.lower() in ["authorization", "x-api-key"]
            }
        }
        return f"cache:{hashlib.sha256(json.dumps(key_data).encode()).hexdigest()}"


class APIGateway:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self.app = FastAPI(title="API Gateway", debug=self.config.debug)
        self.rate_limiter = RateLimiter(
            self.config.redis_url, 
            self.config.rate_limiting
        )
        self.cache = ResponseCache(self.config.redis_url)
        self.http_client = httpx.AsyncClient(timeout=30)
        self._setup_middleware()
        self._setup_routes()
        
    def _load_config(self, config_path: str) -> GatewayConfig:
        """Load configuration from YAML file."""
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
        return GatewayConfig(**config_data)
        
    def _setup_middleware(self):
        """Setup FastAPI middleware."""
        # CORS middleware
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self.config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Rate limiting middleware
        @self.app.middleware("http")
        async def rate_limit_middleware(request: Request, call_next):
            # Skip rate limiting for health checks and metrics
            if request.url.path in ["/health", "/metrics", "/routes"]:
                return await call_next(request)
                
            client_id = ClientIdentifier.identify_client(request)
            route_config = self._find_route_config(request)
            
            if route_config and route_config.rate_limit:
                is_limited, rate_info = await self.rate_limiter.is_rate_limited(
                    client_id, request.url.path, route_config.rate_limit
                )
                
                if is_limited:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "Rate limit exceeded",
                            "detail": f"Limit: {rate_info['limit']} requests per {rate_info['window']} seconds",
                            "retry_after": rate_info['reset'] - int(time.time())
                        },
                        headers={
                            "X-RateLimit-Limit": str(rate_info['limit']),
                            "X-RateLimit-Remaining": str(rate_info['remaining']),
                            "X-RateLimit-Reset": str(rate_info['reset']),
                            "Retry-After": str(rate_info['reset'] - int(time.time()))
                        }
                    )
                    
            return await call_next(request)
            
    def _setup_routes(self):
        """Setup API routes."""
        
        @self.app.get("/health")
        async def health_check():
            return {"status": "healthy", "timestamp": time.time()}
            
        @self.app.get("/metrics")
        async def get_metrics():
            redis_metrics = await self.rate_limiter.get_metrics()
            return {
                "gateway": {
                    "status": "running",
                    "uptime": time.time() - self.start_time
                },
                "rate_limiting": redis_metrics,
                "routes": len(self.config.routes)
            }
            
        @self.app.get("/routes")
        async def get_routes():
            return {
                "routes": [
                    {
                        "path": route.path,
                        "target": route.target,
                        "methods": route.methods
                    }
                    for route in self.config.routes
                ]
            }
            
        # Setup proxy routes for all configured routes
        for route_config in self.config.routes:
            self._create_proxy_route(route_config)
            
    def _create_proxy_route(self, route_config: RouteConfig):
        """Create a proxy route for the given configuration."""
        
        async def proxy_route(request: Request):
            # Check cache first
            if route_config.cache_ttl > 0 and request.method == "GET":
                cache_key = self.cache.generate_key(request)
                cached_response = await self.cache.get(cache_key)
                if cached_response:
                    return JSONResponse(**json.loads(cached_response))
            
            # Build target URL
            target_path = request.url.path
            if route_config.strip_prefix:
                target_path = target_path[len(route_config.path):] or "/"
                
            target_url = f"{route_config.target}{target_path}"
            if request.url.query:
                target_url += f"?{request.url.query}"
                
            # Prepare headers (remove hop-by-hop headers)
            headers = {}
            for key, value in request.headers.items():
                if key.lower() not in [
                    'host', 'content-length', 'connection', 
                    'keep-alive', 'proxy-authenticate', 'proxy-authorization',
                    'te', 'trailers', 'transfer-encoding', 'upgrade'
                ]:
                    headers[key] = value
                    
            # Make request to backend
            try:
                response = await self.http_client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=await request.body(),
                    timeout=route_config.timeout
                )
                
                # Cache response if applicable
                if (route_config.cache_ttl > 0 and 
                    request.method == "GET" and 
                    response.status_code == 200):
                    cache_key = self.cache.generate_key(request)
                    cache_data = json.dumps({
                        "status_code": response.status_code,
                        "content": response.text,
                        "headers": dict(response.headers)
                    })
                    await self.cache.set(cache_key, cache_data.encode(), route_config.cache_ttl)
                
                # Return response
                return JSONResponse(
                    content=response.json() if response.headers.get("content-type") == "application/json" else response.text,
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )
                
            except httpx.TimeoutException:
                raise HTTPException(status_code=504, detail="Backend service timeout")
            except httpx.ConnectError:
                raise HTTPException(status_code=502, detail="Backend service unavailable")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Gateway error: {str(e)}")
                
        # Add route to FastAPI
        for method in route_config.methods:
            self.app.add_route(
                route_config.path, 
                proxy_route, 
                methods=[method]
            )
            
    def _find_route_config(self, request: Request) -> Optional[RouteConfig]:
        """Find route configuration for the given request."""
        for route in self.config.routes:
            if (request.url.path.startswith(route.path) and 
                request.method in route.methods):
                return route
        return None
        
    async def startup(self):
        """Startup tasks."""
        self.start_time = time.time()
        # Test Redis connection
        try:
            await self.rate_limiter.redis.ping()
            print("✅ Redis connected successfully")
        except Exception as e:
            print(f"❌ Redis connection failed: {e}")
            
    async def shutdown(self):
        """Shutdown tasks."""
        await self.http_client.aclose()
        await self.rate_limiter.redis.close()
        await self.cache.redis.close()


# Create FastAPI application
gateway = APIGateway()
app = gateway.app

@app.on_event("startup")
async def startup_event():
    await gateway.startup()

@app.on_event("shutdown")
async def shutdown_event():
    await gateway.shutdown()


if __name__ == "__main__":
    import uvicorn
    config = GatewayConfig()
    uvicorn.run(
        "gateway:app",
        host=config.host,
        port=config.port,
        reload=config.debug
    )
