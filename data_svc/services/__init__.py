"""Shared service-layer modules — business logic that both the gRPC and REST
transports call into. Routers/handlers must remain dumb adapters; anything
that decides, validates, or composes belongs here."""
