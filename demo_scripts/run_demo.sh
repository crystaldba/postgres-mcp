#!/bin/bash

# Postgres Pro MCP Demo Runner
# This script makes it easy to run any of the Postgres Pro MCP demos

# Set default values
DEMO_DIR="$(dirname "$0")"
SQL_DIR="${DEMO_DIR}/sql"
PSQL_OPTIONS="-P pager=off"
DB_PREFIX="mcp_demo_"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Function to display usage information
show_help() {
    echo -e "${BLUE}Postgres Pro MCP Demo Runner${NC}"
    echo "Usage: $0 [options] <demo_number>"
    echo ""
    echo "Options:"
    echo "  -h, --help            Show this help message"
    echo "  -l, --list            List available demos"
    echo "  -c, --cleanup         Clean up (drop) demo databases"
    echo "  -d, --db-prefix NAME  Use NAME as database prefix (default: mcp_demo_)"
    echo "  -s, --skip-setup      Skip the initial database setup (use existing database)"
    echo ""
    echo "Available demos:"
    echo "  1  Database Health Assessment"
    echo "  2  Index Recommendations for a Slow Application"
    echo "  3  Query Optimization with Hypothetical Indexes"
    echo "  4  Schema Discovery and Documentation"
    echo "  5  Storage Optimization"
    echo "  6  Advanced Workload Analysis"
    echo "  7  E-commerce Performance Optimization (advanced)"
    echo "  8  Data Warehouse Query Optimization (advanced)"
    echo "  9  Database Migration Validation (advanced)"
    echo "  10 Time-Series Data Management (advanced)"
    echo ""
    echo "Examples:"
    echo "  $0 1                  Run Demo 1: Database Health Assessment"
    echo "  $0 --cleanup          Clean up all demo databases"
    echo "  $0 --skip-setup 3     Run Demo 3 using an existing database"
    echo ""
}

# Function to list available demos
list_demos() {
    echo -e "${BLUE}Available Postgres Pro MCP Demos:${NC}"
    echo "1. Database Health Assessment - Identify and fix common database health issues"
    echo "2. Index Recommendations - Optimize query performance with intelligent indexing"
    echo "3. Hypothetical Index Testing - Test index improvements without creating them"
    echo "4. Schema Discovery - Explore and document complex database schemas"
    echo "5. Storage Optimization - Reduce database size and improve storage efficiency"
    echo "6. Workload Analysis - Analyze query patterns for scaling and optimization"
    echo ""
    echo -e "${PURPLE}Advanced Multi-Step Demos:${NC}"
    echo "7. E-commerce Performance Optimization - Optimize a growing e-commerce platform"
    echo "8. Data Warehouse Query Optimization - Speed up slow analytical queries"
    echo "9. Database Migration Validation - Verify and optimize after migration"
    echo "10. Time-Series Data Management - Handle IoT sensor data efficiently"
    echo ""
}

# Function to clean up demo databases
cleanup_demos() {
    echo -e "${YELLOW}Cleaning up demo databases...${NC}"
    
    # Get a list of existing demo databases
    databases=$(psql -t -c "\l" | grep "$DB_PREFIX" | awk '{print $1}')
    
    if [ -z "$databases" ]; then
        echo "No demo databases found."
        return
    fi
    
    echo "The following demo databases will be dropped:"
    for db in $databases; do
        echo " - $db"
    done
    
    read -p "Are you sure you want to proceed? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        for db in $databases; do
            echo "Dropping database: $db"
            dropdb "$db" && echo -e " ${GREEN}✓${NC} Dropped $db" || echo -e " ${RED}✗${NC} Failed to drop $db"
        done
    else
        echo "Cleanup cancelled."
    fi
}

# Function to run a specific demo
run_demo() {
    local demo_number=$1
    local skip_setup=$2
    
    case $demo_number in
        1)
            demo_name="health_assessment"
            demo_title="Database Health Assessment"
            db_name="${DB_PREFIX}health"
            ;;
        2)
            demo_name="index_recommendations"
            demo_title="Index Recommendations for a Slow Application"
            db_name="${DB_PREFIX}slow_app"
            ;;
        3)
            demo_name="query_optimization"
            demo_title="Query Optimization with Hypothetical Indexes"
            db_name="${DB_PREFIX}query"
            ;;
        4)
            demo_name="schema_discovery"
            demo_title="Schema Discovery and Documentation"
            db_name="${DB_PREFIX}schema"
            ;;
        5)
            demo_name="storage_optimization"
            demo_title="Storage Optimization"
            db_name="${DB_PREFIX}storage"
            ;;
        6)
            demo_name="workload_analysis"
            demo_title="Advanced Workload Analysis"
            db_name="${DB_PREFIX}workload"
            ;;
        7)
            demo_name="ecommerce_optimization"
            demo_title="E-commerce Performance Optimization"
            db_name="${DB_PREFIX}ecommerce"
            setup_file="${DEMO_DIR}/advanced_demo1_setup.sql"
            ;;
        8)
            demo_name="datawarehouse_optimization"
            demo_title="Data Warehouse Query Optimization"
            db_name="${DB_PREFIX}datawarehouse"
            setup_file="${DEMO_DIR}/advanced_demo2_setup.sql"
            ;;
        9)
            demo_name="migration_validation"
            demo_title="Database Migration Validation"
            db_name="${DB_PREFIX}migration"
            setup_file="${DEMO_DIR}/advanced_demo3_setup.sql"
            ;;
        10)
            demo_name="timeseries_iot"
            demo_title="Time-Series Data Management"
            db_name="${DB_PREFIX}timeseries"
            setup_file="${DEMO_DIR}/advanced_demo4_setup.sql"
            ;;
        *)
            echo -e "${RED}Invalid demo number: $demo_number${NC}"
            show_help
            exit 1
            ;;
    esac
    
    # Setup SQL file path for basic demos
    if [ -z "$setup_file" ]; then
        setup_file="${SQL_DIR}/demo${demo_number}_setup.sql"
    fi
    
    echo -e "${BLUE}Running Demo $demo_number: $demo_title${NC}"
    
    # Check if the database already exists
    if psql -lqt | cut -d \| -f 1 | grep -qw "$db_name"; then
        if [ "$skip_setup" = false ]; then
            echo -e "${YELLOW}Database $db_name already exists.${NC}"
            read -p "Do you want to drop and recreate it? (y/n) " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                echo "Dropping database $db_name..."
                dropdb "$db_name" && echo -e " ${GREEN}✓${NC} Dropped $db_name" || { echo -e " ${RED}✗${NC} Failed to drop $db_name"; exit 1; }
            else
                skip_setup=true
                echo "Using existing database $db_name."
            fi
        else
            echo "Using existing database $db_name."
        fi
    fi
    
    # Create and set up the database if not skipping setup
    if [ "$skip_setup" = false ]; then
        echo "Creating new database $db_name..."
        createdb "$db_name" && echo -e " ${GREEN}✓${NC} Created $db_name" || { echo -e " ${RED}✗${NC} Failed to create $db_name"; exit 1; }
        
        # Check if the setup file exists
        if [ ! -f "$setup_file" ]; then
            echo -e "${RED}Setup file not found: $setup_file${NC}"
            exit 1
        fi
        
        echo "Running setup script..."
        psql $PSQL_OPTIONS -d "$db_name" -f "$setup_file" && echo -e " ${GREEN}✓${NC} Setup complete" || { echo -e " ${RED}✗${NC} Setup failed"; exit 1; }
    fi
    
    echo -e "${GREEN}Demo $demo_number setup is complete!${NC}"
    echo ""
    echo -e "${CYAN}Next steps:${NC}"
    echo "1. Connect to the database with your AI assistant or PostgreSQL client:"
    echo "   psql $db_name"
    echo ""
    echo "2. Ask the demo questions from the Demos.md file for Demo $demo_number"
    echo ""
    echo "3. When finished, you can clean up the database with:"
    echo "   $0 --cleanup"
    echo ""
}

# Parse command line arguments
skip_setup=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -l|--list)
            list_demos
            exit 0
            ;;
        -c|--cleanup)
            cleanup_demos
            exit 0
            ;;
        -d|--db-prefix)
            DB_PREFIX="$2"
            shift
            ;;
        -s|--skip-setup)
            skip_setup=true
            ;;
        *)
            # Assuming it's a demo number
            if [[ $1 =~ ^[0-9]+$ ]]; then
                demo_number=$1
            else
                echo -e "${RED}Unknown option: $1${NC}"
                show_help
                exit 1
            fi
            ;;
    esac
    shift
done

# If no demo number was provided, show help
if [ -z "$demo_number" ] && [ "$skip_setup" = false ]; then
    show_help
    exit 0
fi

# Run the specified demo
if [ -n "$demo_number" ]; then
    run_demo "$demo_number" "$skip_setup"
fi 
