#include "emu.h"
// #include <Arduino.h>

#include <cstdint>
#include <cstdio>
#include "definitions.h"
#include "ppu.h"
#include "apu.h"

// Calling-convention trace output. OFF by default (keeps the browser console and
// pc_emu.exe stdout clean and avoids a printf on every PSH/POP/CAL/RET). Build with
// -DEMU_TRACE to re-enable when debugging the stack/ABI.
#ifdef EMU_TRACE
#define EMU_TRACE_PRINT(...) printf(__VA_ARGS__)
#else
#define EMU_TRACE_PRINT(...) ((void)0)
#endif

// Executed-instruction counter. Used only to measure codegen optimizations; gated behind a
// compile-time macro so production builds (firmware/WASM) pay nothing. Build pc_emu with
// -DEMU_COUNT_INSTRUCTIONS to enable.
#ifdef EMU_COUNT_INSTRUCTIONS
static uint32_t g_instr_count = 0;
#define EMU_INSTR_TICK() (++g_instr_count)
void emu_reset_instruction_count() { g_instr_count = 0; }
uint32_t emu_instruction_count() { return g_instr_count; }
#else
#define EMU_INSTR_TICK() ((void)0)
void emu_reset_instruction_count() {}
uint32_t emu_instruction_count() { return 0; }
#endif

cpu cpu_instance;

void set_flags(uint32_t result)
{
    cpu_instance.flags = 0; // Reset flags
    if ((result & 0xFFFF) == 0)
        cpu_instance.flags |= 0x01; // Zero flag
    if (result & 0x8000)            // Check if the result is negative (sign flag)
        cpu_instance.flags |= 0x02; // Sign flag
    if (result > 0xFFFF)            // Check for carry
        cpu_instance.flags |= 0x04; // Carry flag
}

uint16_t read_word_le(uint16_t address)
{
    return static_cast<uint16_t>(cpu_instance.memory[address]) |
           (static_cast<uint16_t>(cpu_instance.memory[address + 1]) << 8);
}

void write_word_le(uint16_t address, uint16_t value)
{
    cpu_instance.memory[address] = static_cast<uint8_t>(value & 0x00FF);
    cpu_instance.memory[address + 1] = static_cast<uint8_t>((value >> 8) & 0x00FF);
}

// Syscall handler — null by default (no-op).  Set via emu_set_syscall_handler().
static void (*g_syscall_handler)(uint16_t) = nullptr;

void emu_set_syscall_handler(void (*fn)(uint16_t))
{
    g_syscall_handler = fn;
}

// Frame-yield ("present") flag. The PRESENT syscall is handled here in the core, not by a host
// handler: it halts the CPU so the host can display the completed frame and pace to its refresh
// rate, and the next frame resumes the CPU where it left off. Uniform across firmware/PC/WASM.
static bool g_present = false;

bool emu_present_pending() { return g_present; }

// Resume the CPU if the previous frame ended on a PRESENT yield. Called at the start of each host frame.
void emu_begin_frame()
{
    if (g_present)
    {
        cpu_instance.running = true;
        g_present = false;
    }
}

// Trigger a frame yield from a syscall handler. sys_ppu_submit calls this when its command stream
// ends in PRESENT: behaves exactly like the SYSCALL_PRESENT path — halt now so the host displays
// the composed frame and paces, then resume next frame. Keeps the PPU present uniform with the
// legacy sys_present across firmware / PC / WASM.
void emu_request_present()
{
    g_present = true;
    cpu_instance.running = false;
}

// Centralized store helpers. All guest memory writes for store opcodes go through
// these so the SYSCALL_PORT hook fires regardless of which opcode triggered the write.
static inline void mem_store_byte(uint16_t addr, uint8_t v)
{
    cpu_instance.memory[addr] = v;
    if (addr == SYSCALL_PORT)
    {
        if (v == SYSCALL_PRESENT)            // frame yield: core-handled, resumes next frame
        {
            g_present = true;
            cpu_instance.running = false;
        }
        else if (g_syscall_handler)
            g_syscall_handler(static_cast<uint16_t>(v));
    }
}

static inline void mem_store_word(uint16_t addr, uint16_t v)
{
    cpu_instance.memory[addr]     = static_cast<uint8_t>(v & 0xFF);
    cpu_instance.memory[addr + 1] = static_cast<uint8_t>((v >> 8) & 0xFF);
    if (addr == SYSCALL_PORT)
    {
        if (v == SYSCALL_PRESENT)
        {
            g_present = true;
            cpu_instance.running = false;
        }
        else if (g_syscall_handler)
            g_syscall_handler(v);
    }
}

uint16_t read_immediate16_i4()
{
    return read_word_le(static_cast<uint16_t>(cpu_instance.pc + 2));
}

enum class alu_op
{
    add,
    sub,
    bit_and,
    bit_or,
    bit_xor,
    shl,
    shr,
    mul,
    div
};

uint32_t apply_word_alu(uint16_t lhs, uint16_t rhs, alu_op op)
{
    switch (op)
    {
    case alu_op::add:
        return static_cast<uint32_t>(lhs) + rhs;
    case alu_op::sub:
        return static_cast<uint32_t>(lhs) - rhs;
    case alu_op::bit_and:
        return lhs & rhs;
    case alu_op::bit_or:
        return lhs | rhs;
    case alu_op::bit_xor:
        return lhs ^ rhs;
    case alu_op::shl:
        return static_cast<uint32_t>(lhs) << (rhs & 0x0F);
    case alu_op::shr:
        return static_cast<uint32_t>(lhs) >> (rhs & 0x0F);
    case alu_op::mul:
        return static_cast<uint32_t>(lhs) * static_cast<uint32_t>(rhs);
    case alu_op::div:
        return (rhs == 0) ? 0xFFFF : (static_cast<uint32_t>(lhs) / rhs);
    }

    return 0;
}

uint32_t apply_byte_alu(uint8_t lhs, uint8_t rhs, alu_op op)
{
    switch (op)
    {
    case alu_op::add:
        return static_cast<uint32_t>(lhs) + rhs;
    case alu_op::sub:
        return static_cast<uint32_t>(lhs) - rhs;
    case alu_op::bit_and:
        return lhs & rhs;
    case alu_op::bit_or:
        return lhs | rhs;
    case alu_op::bit_xor:
        return lhs ^ rhs;
    case alu_op::shl:
        return static_cast<uint32_t>(lhs) << (rhs & 0x07);
    case alu_op::shr:
        return static_cast<uint32_t>(lhs) >> (rhs & 0x07);
    case alu_op::mul:
        return static_cast<uint32_t>(lhs) * static_cast<uint32_t>(rhs);
    case alu_op::div:
        return (rhs == 0) ? 0xFF : (static_cast<uint32_t>(lhs) / rhs);
    }

    return 0;
}

void execute_alu(alu_op op, uint16_t register1, uint16_t register2, uint16_t size_flag, uint16_t lower_flag, uint16_t mem_flag, uint16_t abs_flag)
{
    uint32_t result;

    if (size_flag == 0 && lower_flag == 0) // 4-byte I-type instruction (reg OP= imm16)
    {
        uint16_t full_immediate = read_immediate16_i4();
        result = apply_word_alu(cpu_instance.registers[register1].word, full_immediate, op);
        cpu_instance.registers[register1].word = static_cast<uint16_t>(result);
        set_flags(result);
        cpu_instance.pc += 4;
        return;
    }

    if (size_flag == 0 && lower_flag == 1) // 2-byte R-type word instruction or 4-byte M-type
    {
        if (mem_flag == 0)
        {
            result = apply_word_alu(cpu_instance.registers[register1].word, cpu_instance.registers[register2].word, op);
            cpu_instance.registers[register1].word = static_cast<uint16_t>(result);
            set_flags(result);
            cpu_instance.pc += 2;
        }
        else
        {
            // M-type: abs_flag=1 -> immediate IS the address (base reg ignored);
            //         abs_flag=0 -> base register + signed offset (legacy behavior).
            uint16_t imm = read_immediate16_i4();
            uint16_t address = abs_flag ? imm
                                        : static_cast<uint16_t>(cpu_instance.registers[register2].word
                                                                + static_cast<int16_t>(imm));
            uint16_t mem_val = read_word_le(address);
            result = apply_word_alu(cpu_instance.registers[register1].word, mem_val, op);
            cpu_instance.registers[register1].word = static_cast<uint16_t>(result);
            set_flags(result);
            cpu_instance.pc += 4;
        }
        return;
    }

    // R-type byte instruction
    if (lower_flag == 0)
    {
        result = apply_byte_alu(cpu_instance.registers[register1].upper, cpu_instance.registers[register2].upper, op);
        cpu_instance.registers[register1].upper = static_cast<uint8_t>(result);
    }
    else
    {
        result = apply_byte_alu(cpu_instance.registers[register1].lower, cpu_instance.registers[register2].lower, op);
        cpu_instance.registers[register1].lower = static_cast<uint8_t>(result);
    }

    set_flags(result);
    cpu_instance.pc += 2;
}

void initialize_cpu()
{
    emu_reset_instruction_count();
    g_present = false;
    cpu_instance.running = true;
    cpu_instance.pc = 0;
    cpu_instance.registers[7].word = STACK_START_ADDRESS; // Initialize stack pointer (R7) to the top of memory
    cpu_instance.flags = 0;
    for (int i = 0; i < 8; ++i)
    {
        cpu_instance.registers[i].word = 0;
    }
    for (int i = 0; i <= MAX_RAM_ADDRESS; ++i)
    {
        cpu_instance.memory[i] = 0;
    }
    ppu_reset();   // reset the PPU unit alongside the CPU (every host routes through here)
    apu_reset();   // reset the APU unit alongside the CPU (same reasoning)
}

void decode_and_execute(uint16_t instruction)
{
    /*
    Instruction formats:
    1. R-type (Register):
        - Opcode: 5 bits (bits 11-15)
        - Register 1: 3 bits (bits 8-10)
        - Register 2: 3 bits (bits 5-7)
        - Size flag: 1 bit (bit 4)
        - Lower flag: 1 bit (bit 3)
        - Unused: 3 bits (bits 0-2)
    2. 2 byte I-type (Immediate):
        - Opcode: 5 bits (bits 11-15)
        - Register 1: 3 bits (bits 8-10)
        - Immediate: 8 bits (bits 0-7)
    3. 4 byte I-type (Immediate):
        - Opcode: 5 bits (bits 11-15)
        - Register 1: 3 bits (bits 8-10)
        - Remaining 8 bits in instruction word: opcode-specific control/unused bits (bit 4 is size flag, bit 3 is lower flag)
        - Immediate: 16 bits (next two bytes in memory, little-endian)
    4. 4 byte M-type (Memory):
        - Opcode: 5 bits (bits 11-15)
        - Register 1: 3 bits (bits 8-10)
        - Register 2: 3 bits (bits 5-7) (Base register)
        - Size flag: 1 bit (bit 4) (always 0)
        - Lower flag: 1 bit (bit 3) (always 1)
        - Mem flag: 1 bit (bit 2) (always 1)
        - Unused: 2 bits (bits 0-1)
        - Immediate: 16 bits (next two bytes in memory, signed offset)
     */
    EMU_INSTR_TICK();
    uint16_t opcode = (instruction >> 11) & 0x1F;   // Extract the opcode (higher 5 bits)
    uint16_t register1 = (instruction >> 8) & 0x07; // Extract the first register (bits 8-10)
    uint16_t register2 = (instruction >> 5) & 0x07; // Extract the second register (bits 5-7)
    uint16_t size_flag = (instruction >> 4) & 0x1;  // Extract the size flag (bit 4)
    uint16_t lower_flag = (instruction >> 3) & 0x1; // Extract the lower flag (bit 3)
    uint16_t mem_flag = (instruction >> 2) & 0x1;   // Extract the memory flag (bit 2)
    uint16_t abs_flag = (instruction >> 1) & 0x1;   // M-type/LDROFF absolute-address flag (bit 1)
    uint16_t immediate = instruction & 0xFF;        // Extract the 8 bit immediate value (lower 8 bits)
    switch (opcode)
    {
    case 0x00: // NOP
    {
        cpu_instance.pc += 2; // Move to the next instruction
        break;
    }
    case 0x01: // Halt
    {
        cpu_instance.running = false;
        cpu_instance.pc += 2; // Move to the next instruction
        break;
    }
    case 0x02: // Load immediate to lower LDI_LO (2 byte I-type)
    {
        cpu_instance.registers[register1].lower = immediate;
        cpu_instance.pc += 2; // Move to the next instruction
        break;
    }
    case 0x03: // Load immediate to upper LDI_HI (2 byte I-type)
    {
        cpu_instance.registers[register1].upper = immediate;
        cpu_instance.pc += 2; // Move to the next instruction
        break;
    }
    case 0x04: // Load immediate to register LDI (4 byte I-type)
    {
        uint16_t full_immediate = read_immediate16_i4();
        cpu_instance.registers[register1].word = full_immediate;
        cpu_instance.pc += 4; // Move to the next instruction
        break;
    }
    case 0x05: // Load register to immediate memory STRI (4 byte I-type)
    {
        uint16_t address = read_immediate16_i4();
        mem_store_word(address, cpu_instance.registers[register1].word);
        cpu_instance.pc += 4;
        break;
    }
    case 0x06: // Load register to register pointed memory STR (R-type)
    {
        uint16_t addr = cpu_instance.registers[register2].word;
        if (size_flag == 0) // word operation
        {
            mem_store_word(addr, cpu_instance.registers[register1].word);
        }
        else // byte operation
        {
            uint8_t byte_val = (lower_flag == 0) ? cpu_instance.registers[register1].upper
                                                 : cpu_instance.registers[register1].lower;
            mem_store_byte(addr, byte_val);
        }
        cpu_instance.pc += 2;
        break;
    }
    case 0x07: // Load memory to register LDR (R-type)
    {
        if (size_flag == 0) // If size flag is 0, it's a word operation
        {
            // Read the base address once so a self-base load (rd == rb, used by load_ptr) stays
            // correct — writing register1.lower must not perturb the address for the upper byte.
            uint16_t addr = cpu_instance.registers[register2].word;
            cpu_instance.registers[register1].lower = cpu_instance.memory[addr];     // Load lower byte
            cpu_instance.registers[register1].upper = cpu_instance.memory[addr + 1]; // Load upper byte
        }
        else // If size flag is 1, it's a byte operation
        {
            if (lower_flag == 0) // If lower flag is 0, load the upper byte
            {
                cpu_instance.registers[register1].upper = cpu_instance.memory[cpu_instance.registers[register2].word];
            }
            else // If lower flag is 1, load the lower byte
            {
                cpu_instance.registers[register1].lower = cpu_instance.memory[cpu_instance.registers[register2].word];
            }
        }

        cpu_instance.pc += 2; // Move to the next instruction
        break;
    }
    case 0x08: // Load register to register MOV (R-type)
    {
        if (size_flag == 0) // If size flag is 0, it's a word operation
        {
            cpu_instance.registers[register1].word = cpu_instance.registers[register2].word;
            // Debug: detect frame-pointer/stack-pointer moves
            if (register1 == 7 && register2 == 6)
            {
                EMU_TRACE_PRINT("TRACE MOV SP,FP -> SP=0x%04X FP=0x%04X PC=0x%04X\n", cpu_instance.registers[register1].word, cpu_instance.registers[register2].word, cpu_instance.pc);
            }
            if (register1 == 6 && register2 == 7)
            {
                EMU_TRACE_PRINT("TRACE MOV FP,SP -> FP=0x%04X SP=0x%04X PC=0x%04X\n", cpu_instance.registers[register1].word, cpu_instance.registers[register2].word, cpu_instance.pc);
            }
        }
        else // If size flag is 1, it's a byte operation
        {
            if (lower_flag == 0) // If lower flag is 0, move the upper byte
            {
                cpu_instance.registers[register1].upper = cpu_instance.registers[register2].upper;
            }
            else // If lower flag is 1, move the lower byte
            {
                cpu_instance.registers[register1].lower = cpu_instance.registers[register2].lower;
            }
        }
        cpu_instance.pc += 2; // Move to the next instruction
        break;
    }
    case 0x09: // Add register to register or immediate ADD (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::add, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x0A: // Subtract register from register or immediate SUB (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::sub, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x0B: // Bitwise AND register with register or immediate AND (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::bit_and, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x0C: // Bitwise OR register with register or immediate OR (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::bit_or, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x0D: // Bitwise XOR register with register or immediate XOR (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::bit_xor, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x0E: // Logical shift left register by register or immediate SHL (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::shl, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x0F: // Logical shift right register by register or immediate SHR (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::shr, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x10: // Compare register with register or immediate CMP (R-type or 4 byte I-type)
    {
        uint32_t result;
        if (size_flag == 0 && lower_flag == 0) // 4-byte I-type instruction (reg CMP imm16)
        {
            uint16_t full_immediate = read_immediate16_i4();
            result = cpu_instance.registers[register1].word - full_immediate;
            set_flags(result);
            cpu_instance.pc += 4;
        }
        else if (size_flag == 0 && lower_flag == 1) // 2-byte R-type word or 4-byte M-type
        {
            if (mem_flag == 0)
            {
                result = cpu_instance.registers[register1].word - cpu_instance.registers[register2].word;
                set_flags(result);
                cpu_instance.pc += 2;
            }
            else
            {
                // M-type CMP: abs_flag=1 -> immediate is the absolute address (base reg ignored).
                uint16_t imm = read_immediate16_i4();
                uint16_t address = abs_flag ? imm
                                            : static_cast<uint16_t>(cpu_instance.registers[register2].word
                                                                    + static_cast<int16_t>(imm));
                uint16_t mem_val = read_word_le(address);
                result = cpu_instance.registers[register1].word - mem_val;
                set_flags(result);
                cpu_instance.pc += 4;
            }
        }
        else if (size_flag == 1) // R-type byte instruction
        {
            if (lower_flag == 0)
            {
                result = cpu_instance.registers[register1].upper - cpu_instance.registers[register2].upper;
                set_flags(result);
            }
            else
            {
                result = cpu_instance.registers[register1].lower - cpu_instance.registers[register2].lower;
                set_flags(result);
            }
            cpu_instance.pc += 2;
        }
        break;
    }
    case 0x11: // Jump to address if zero flag is set JZ (4 byte I-type)
    {
        if (cpu_instance.flags & 0x01) // Check if zero flag is set
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        }
        else
        {
            cpu_instance.pc += 4; // Move to the next instruction
        }
        break;
    }

    case 0x12: // Jump to address if zero flag is not set JNZ (4 byte I-type)
    {
        if (!(cpu_instance.flags & 0x01)) // Check if zero flag is not set
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        }
        else
        {
            cpu_instance.pc += 4; // Move to the next instruction
        }
        break;
    }
    case 0x13: // Jump to address if sign flag is set JS (4 byte I-type)
    {
        if (cpu_instance.flags & 0x02) // Check if sign flag is set
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        }
        else
        {
            cpu_instance.pc += 4; // Move to the next instruction
        }
        break;
    }
    case 0x14: // Jump to address if sign flag is not set JNS (4 byte I-type)
    {
        if (!(cpu_instance.flags & 0x02)) // Check if sign flag is not set
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        }
        else
        {
            cpu_instance.pc += 4; // Move to the next instruction
        }
        break;
    }
    case 0x15: // Jump to address if carry flag is set JC (4 byte I-type)
    {
        if (cpu_instance.flags & 0x04) // Check if carry flag is set
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        }
        else
        {
            cpu_instance.pc += 4; // Move to the next instruction
        }
        break;
    }
    case 0x16: // Jump to address if carry flag is not set JNC (4 byte I-type)
    {
        if (!(cpu_instance.flags & 0x04)) // Check if carry flag is not set
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        }
        else
        {
            cpu_instance.pc += 4; // Move to the next instruction
        }
        break;
    }
    case 0x17: // JMP — immediate (label) when lower_flag=0, register (indirect) when lower_flag=1.
    {
        // The 5-bit opcode space is full (0x00-0x1F all used), so the register/indirect jump reuses
        // JMP with the lower_flag bit (bit 3) — mirroring the ALU R-type-vs-I-type convention. Every
        // existing ROM emits JMP with lower_flag=0, so this is backward-compatible; the jump-table
        // codegen sets the bit to jump to a computed target in a register (O(1) switch dispatch).
        if (lower_flag) // register mode (2-byte R-type): pc = R[reg1]
        {
            cpu_instance.pc = cpu_instance.registers[register1].word;
        }
        else            // immediate mode (4-byte I-type): pc = imm16
        {
            uint16_t full_immediate = read_immediate16_i4();
            cpu_instance.pc = full_immediate;
        }
        break;
    }
    case 0x18: // push register to stack PSH (R-type)
    {
        cpu_instance.registers[7].word -= 2;                                                     // Move stack pointer (R7) down
        write_word_le(cpu_instance.registers[7].word, cpu_instance.registers[register1].word); // Push word (little-endian)
        // Debug trace
        EMU_TRACE_PRINT("TRACE PSH R%d val=0x%04X SP=0x%04X\n", register1, cpu_instance.registers[register1].word, cpu_instance.registers[7].word);
        cpu_instance.pc += 2;                                                                  // Move to the next instruction
        break;
    }
    case 0x19: // pop register from stack POP (R-type)
    {
        cpu_instance.registers[register1].word = read_word_le(cpu_instance.registers[7].word); // Pop word (little-endian)
        cpu_instance.registers[7].word += 2;                                                  // Move stack pointer (R7) up
        // Debug trace
        EMU_TRACE_PRINT("TRACE POP R%d val=0x%04X SP=0x%04X\n", register1, cpu_instance.registers[register1].word, cpu_instance.registers[7].word);
        // Sanity check: detect stack underflow / wrap-around (floor = CODE_START; the stack lives
        // high and must never drop into the code/data region below it)
        if (cpu_instance.registers[7].word < STACK_FLOOR)
        {
            EMU_TRACE_PRINT("ERROR: Stack underflow/wrap detected at PC=0x%04X after POP (SP=0x%04X)\n", cpu_instance.pc, cpu_instance.registers[7].word);
            cpu_instance.running = false;
            return;
        }
        cpu_instance.pc += 2;                                                                 // Move to the next instruction
        break;
    }
    case 0x1A: // Call subroutine at address CAL (4 byte I-type)
    {
        uint16_t full_immediate = read_immediate16_i4();
        // Push return address onto stack
        cpu_instance.registers[7].word -= 2;                                                       // Move stack pointer (R7) down
        write_word_le(cpu_instance.registers[7].word, static_cast<uint16_t>(cpu_instance.pc + 4)); // Push return address (little-endian)
        // Debug trace
        EMU_TRACE_PRINT("TRACE CAL addr=0x%04X retaddr=0x%04X SP=0x%04X\n", full_immediate, (uint16_t)(cpu_instance.pc+4), cpu_instance.registers[7].word);
        EMU_TRACE_PRINT("      MEM[SP]=0x%04X MEM[SP+2]=0x%04X\n", read_word_le(cpu_instance.registers[7].word), read_word_le(cpu_instance.registers[7].word + 2));
        cpu_instance.pc = full_immediate;                                                          // Jump to the subroutine address
        break;
    }
    case 0x1B: // Return from subroutine RET (R-type)
    {
        uint16_t sp_addr = cpu_instance.registers[7].word;
        EMU_TRACE_PRINT("TRACE RET: SP=0x%04X MEM[SP]=0x%04X MEM[SP+2]=0x%04X\n", sp_addr, read_word_le(sp_addr), read_word_le(sp_addr + 2));
        uint16_t return_address = read_word_le(sp_addr); // Pop return address (little-endian)
        cpu_instance.registers[7].word += 2;                                    // Move stack pointer (R7) up
        EMU_TRACE_PRINT("      RETADDR=0x%04X SP(after pop)=0x%04X\n", return_address, cpu_instance.registers[7].word);
        cpu_instance.pc = return_address;                                       // Jump back to the return address
        break;
    }
    case 0x1C: // Load from base+offset LDROFF (4 byte I-type)
    {
        // Format: first word has opcode (5), dst (3), base (3), abs flag (bit 1)
        // Second word has the signed offset, or the absolute address when abs_flag is set.
        uint16_t imm = read_immediate16_i4();
        uint16_t address = abs_flag ? imm
                                    : static_cast<uint16_t>(cpu_instance.registers[register2].word
                                                            + static_cast<int16_t>(imm));
        cpu_instance.registers[register1].word = read_word_le(address);
        cpu_instance.pc += 4;
        break;
    }
    case 0x1D: // Store to base+offset STROFF (4 byte I-type)
    {
        // Format: first word has opcode (5), src (3), base (3), unused
        // Second word has signed 16-bit offset
        uint16_t offset_imm = read_immediate16_i4();
        int16_t signed_offset = static_cast<int16_t>(offset_imm);
        uint16_t address = cpu_instance.registers[register2].word + signed_offset;
        mem_store_word(address, cpu_instance.registers[register1].word);
        cpu_instance.pc += 4;
        break;
    }
    case 0x1E: // Multiply register by register or immediate MUL (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::mul, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    case 0x1F: // Divide register by register or immediate DIV (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::div, register1, register2, size_flag, lower_flag, mem_flag, abs_flag);
        break;
    }
    default:
        // Handle unknown opcode
        break;
    }
}

bool run_frame_instructions()
{
    emu_begin_frame();              // resume if the previous frame ended on a PRESENT yield
    int instruction_count = 0;

    while (cpu_instance.running && instruction_count < 100000)
    {
        uint16_t instruction = read_word_le(cpu_instance.pc);
        decode_and_execute(instruction);
        instruction_count++;
    }
    return cpu_instance.running;
}