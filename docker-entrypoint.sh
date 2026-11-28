#!/bin/bash

# Don't exit immediately so we can debug issues
# set -e

# Describe a connection string for diagnostics without revealing credentials.
# The string is parsed by libpq itself (through psycopg), so URIs, key=value
# strings, quoting and escaping are all handled exactly as the server's client
# library handles them. Only a fixed set of non-secret fields is printed. If the
# string cannot be parsed, or Python is unavailable, nothing from it is printed.
describe_connection() {
    local summary
    summary=$(python - "$1" 2>/dev/null <<'PY'
import sys

from psycopg.conninfo import conninfo_to_dict

SHOWN = ("host", "hostaddr", "port", "dbname", "user", "sslmode")
SECRET = ("password", "sslpassword")

params = conninfo_to_dict(sys.argv[1])
# libpq splits a URI at the first "@", so a password containing an unencoded
# "@" leaves its remainder in the host. A host never contains "@": hide it all.
if any("@" in str(params.get(key, "")) for key in ("host", "hostaddr")):
    sys.exit(1)
parts = [f"{key}={params[key]}" for key in SHOWN if params.get(key) not in (None, "")]
parts += [f"{key}=****" for key in SECRET if params.get(key) not in (None, "")]
text = " ".join(parts)
print("".join(ch if ch.isprintable() else "?" for ch in text))
PY
    )
    if [[ $? -eq 0 && -n "$summary" ]]; then
        echo "<connection: $summary>"
    else
        echo "<connection string hidden>"
    fi
}

# An argument is treated as a connection string if it contains a URI scheme, or
# is a non-option argument containing key=value pairs.
is_connection_arg() {
    [[ "$1" == *"://"* ]] || [[ "$1" != -* && "$1" == *"="* ]]
}

# Function to replace localhost in a string with the Docker host
replace_localhost() {
    local input_str="$1"
    local docker_host=""

    # Try to determine Docker host address
    if ping -c 1 -w 1 host.docker.internal >/dev/null 2>&1; then
        docker_host="host.docker.internal"
        echo "Docker Desktop detected: Using host.docker.internal for localhost" >&2
    elif ping -c 1 -w 1 172.17.0.1 >/dev/null 2>&1; then
        docker_host="172.17.0.1"
        echo "Docker on Linux detected: Using 172.17.0.1 for localhost" >&2
    else
        echo "WARNING: Cannot determine Docker host IP. Using original address." >&2
        return 1
    fi

    # Replace localhost with Docker host
    if [[ -n "$docker_host" ]]; then
        local new_str="${input_str/localhost/$docker_host}"
        echo "  Remapping localhost --> $docker_host" >&2
        echo "$new_str"
        return 0
    fi

    # No replacement made
    echo "$input_str"
    return 1
}

# Create a new array for the processed arguments
processed_args=()
processed_args+=("$1")
shift 1

# Process remaining command-line arguments for postgres:// or postgresql:// URLs that contain localhost
for arg in "$@"; do
    if [[ "$arg" == *"postgres"*"://"*"localhost"* ]]; then
        echo "Found localhost in database connection: $(describe_connection "$arg")" >&2
        new_arg=$(replace_localhost "$arg")
        if [[ $? -eq 0 ]]; then
            processed_args+=("$new_arg")
        else
            processed_args+=("$arg")
        fi
    else
        processed_args+=("$arg")
    fi
done

# Check and replace localhost in DATABASE_URI if it exists
if [[ -n "$DATABASE_URI" && "$DATABASE_URI" == *"postgres"*"://"*"localhost"* ]]; then
    echo "Found localhost in DATABASE_URI: $(describe_connection "$DATABASE_URI")" >&2
    new_uri=$(replace_localhost "$DATABASE_URI")
    if [[ $? -eq 0 ]]; then
        export DATABASE_URI="$new_uri"
    fi
fi

# Check if SSE transport is specified and --sse-host is not already set
has_sse=false
has_sse_host=false

for arg in "${processed_args[@]}"; do
    if [[ "$arg" == "--transport" ]]; then
        # Check next argument for "sse"
        for next_arg in "${processed_args[@]}"; do
            if [[ "$next_arg" == "sse" ]]; then
                has_sse=true
                break
            fi
        done
    elif [[ "$arg" == "--transport=sse" ]]; then
        has_sse=true
    elif [[ "$arg" == "--sse-host"* ]]; then
        has_sse_host=true
    fi
done

# Add --sse-host if needed
if [[ "$has_sse" == true ]] && [[ "$has_sse_host" == false ]]; then
    echo "SSE transport detected, adding --sse-host=0.0.0.0" >&2
    processed_args+=("--sse-host=0.0.0.0")
fi

echo "----------------" >&2
echo "Executing command:" >&2
printed_args=()
for arg in "${processed_args[@]}"; do
    if is_connection_arg "$arg"; then
        printed_args+=("$(describe_connection "$arg")")
    else
        printed_args+=("$arg")
    fi
done
echo "${printed_args[@]}" >&2
echo "----------------" >&2

# Execute the command with the processed arguments
# Use exec to replace the shell with the Python process, making it PID 1
# This ensures signals (SIGTERM, SIGINT) are properly received
exec "${processed_args[@]}"
