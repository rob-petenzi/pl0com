#!/bin/bash
set -e

# Function: run_arm_emulation
# Description: Compiles an ARM assembly file with a C runtime using semihosting, 
#              and executes the resulting binary via QEMU system emulation.
# Arguments:
#   $1 - The target assembly file to compile (.s)
run_arm_emulation() {
    if [ -z "$1" ]; then
        echo "Usage: ./run.sh <file.s>"
        exit 1
    fi

    local FILE="$1"
    local BASENAME=$(basename "$FILE" | cut -d. -f1)
    local RUNTIME="runtime.c"

    local CC="arm-none-eabi-gcc"
    local EMULATOR="qemu-system-arm"

    echo "Compiling, Assembling, and Linking (Bare Metal)..."
    $CC "$RUNTIME" "$FILE" -o "${BASENAME}" --specs=rdimon.specs

    echo "Executing via QEMU System Emulation..."
    echo "--- Output ---"
    
    # Emulate a Versatile PB board, disable graphical output, and enable semihosting
    $EMULATOR -machine versatilepb -cpu arm1176 -m 128M -nographic -semihosting -kernel ./"${BASENAME}"
    
    local EXIT_CODE=$?
    echo "--------------"
    echo "Exited with code: $EXIT_CODE"
}

run_arm_emulation "$1"