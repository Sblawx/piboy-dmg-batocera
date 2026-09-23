FROM arm64v8/debian:bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    gcc libc6-dev make pkg-config \
    libwayland-dev libwayland-bin wayland-protocols \
    ca-certificates curl binutils && rm -rf /var/lib/apt/lists/*
WORKDIR /work
