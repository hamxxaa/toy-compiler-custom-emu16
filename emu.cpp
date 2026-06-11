#include "emu.h"
// #include <Arduino.h>

#include <cstdint>
#include <cstdio>
#include "definitions.h"

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

void execute_alu(alu_op op, uint16_t register1, uint16_t register2, uint16_t size_flag, uint16_t lower_flag, uint16_t mem_flag)
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
            int16_t offset = static_cast<int16_t>(read_immediate16_i4());
            uint16_t address = cpu_instance.registers[register2].word + offset;
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
    uint16_t opcode = (instruction >> 11) & 0x1F;   // Extract the opcode (higher 5 bits)
    uint16_t register1 = (instruction >> 8) & 0x07; // Extract the first register (bits 8-10)
    uint16_t register2 = (instruction >> 5) & 0x07; // Extract the second register (bits 5-7)
    uint16_t size_flag = (instruction >> 4) & 0x1;  // Extract the size flag (bit 4)
    uint16_t lower_flag = (instruction >> 3) & 0x1; // Extract the lower flag (bit 3)
    uint16_t mem_flag = (instruction >> 2) & 0x1;   // Extract the memory flag (bit 2)
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
        cpu_instance.memory[address] = cpu_instance.registers[register1].lower;     // Store lower byte
        cpu_instance.memory[address + 1] = cpu_instance.registers[register1].upper; // Store upper byte
        cpu_instance.pc += 4;                                                       // Move to the next instruction
        break;
    }
    case 0x06: // Load register to register pointed memory STR (R-type)
    {
        if (size_flag == 0) // If size flag is 0, it's a word operation
        {
            cpu_instance.memory[cpu_instance.registers[register2].word] = cpu_instance.registers[register1].lower;     // Store lower byte
            cpu_instance.memory[cpu_instance.registers[register2].word + 1] = cpu_instance.registers[register1].upper; // Store upper byte
        }
        else // If size flag is 1, it's a byte operation
        {
            if (lower_flag == 0) // If lower flag is 0, store the upper byte
            {
                cpu_instance.memory[cpu_instance.registers[register2].word] = cpu_instance.registers[register1].upper;
            }
            else // If lower flag is 1, store the lower byte
            {
                cpu_instance.memory[cpu_instance.registers[register2].word] = cpu_instance.registers[register1].lower;
            }
        }
        // Move to the next instruction
        cpu_instance.pc += 2;
        break;
    }
    case 0x07: // Load memory to register LDR (R-type)
    {
        if (size_flag == 0) // If size flag is 0, it's a word operation
        {
            cpu_instance.registers[register1].lower = cpu_instance.memory[cpu_instance.registers[register2].word];     // Load lower byte
            cpu_instance.registers[register1].upper = cpu_instance.memory[cpu_instance.registers[register2].word + 1]; // Load upper byte
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
                printf("TRACE MOV SP,FP -> SP=0x%04X FP=0x%04X PC=0x%04X\n", cpu_instance.registers[register1].word, cpu_instance.registers[register2].word, cpu_instance.pc);
            }
            if (register1 == 6 && register2 == 7)
            {
                printf("TRACE MOV FP,SP -> FP=0x%04X SP=0x%04X PC=0x%04X\n", cpu_instance.registers[register1].word, cpu_instance.registers[register2].word, cpu_instance.pc);
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
        execute_alu(alu_op::add, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x0A: // Subtract register from register or immediate SUB (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::sub, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x0B: // Bitwise AND register with register or immediate AND (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::bit_and, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x0C: // Bitwise OR register with register or immediate OR (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::bit_or, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x0D: // Bitwise XOR register with register or immediate XOR (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::bit_xor, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x0E: // Logical shift left register by register or immediate SHL (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::shl, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x0F: // Logical shift right register by register or immediate SHR (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::shr, register1, register2, size_flag, lower_flag, mem_flag);
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
                int16_t offset = static_cast<int16_t>(read_immediate16_i4());
                uint16_t address = cpu_instance.registers[register2].word + offset;
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
    case 0x17: // Unconditional jump to address JMP (4 byte I-type)
    {
        uint16_t full_immediate = read_immediate16_i4();
        cpu_instance.pc = full_immediate; // Jump to the address specified by the immediate value
        break;
    }
    case 0x18: // push register to stack PSH (R-type)
    {
        cpu_instance.registers[7].word -= 2;                                                     // Move stack pointer (R7) down
        write_word_le(cpu_instance.registers[7].word, cpu_instance.registers[register1].word); // Push word (little-endian)
        // Debug trace
        printf("TRACE PSH R%d val=0x%04X SP=0x%04X\n", register1, cpu_instance.registers[register1].word, cpu_instance.registers[7].word);
        cpu_instance.pc += 2;                                                                  // Move to the next instruction
        break;
    }
    case 0x19: // pop register from stack POP (R-type)
    {
        cpu_instance.registers[register1].word = read_word_le(cpu_instance.registers[7].word); // Pop word (little-endian)
        cpu_instance.registers[7].word += 2;                                                  // Move stack pointer (R7) up
        // Debug trace
        printf("TRACE POP R%d val=0x%04X SP=0x%04X\n", register1, cpu_instance.registers[register1].word, cpu_instance.registers[7].word);
        // Sanity check: detect stack underflow / wrap-around
        if (cpu_instance.registers[7].word < 0x1000)
        {
            printf("ERROR: Stack underflow/wrap detected at PC=0x%04X after POP (SP=0x%04X)\n", cpu_instance.pc, cpu_instance.registers[7].word);
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
        printf("TRACE CAL addr=0x%04X retaddr=0x%04X SP=0x%04X\n", full_immediate, (uint16_t)(cpu_instance.pc+4), cpu_instance.registers[7].word);
        // Dump memory at new SP (return address written)
        uint16_t sp = cpu_instance.registers[7].word;
        uint16_t w0 = read_word_le(sp);
        uint16_t w1 = read_word_le(sp + 2);
        printf("      MEM[SP]=0x%04X MEM[SP+2]=0x%04X\n", w0, w1);
        cpu_instance.pc = full_immediate;                                                          // Jump to the subroutine address
        break;
    }
    case 0x1B: // Return from subroutine RET (R-type)
    {
        uint16_t sp_addr = cpu_instance.registers[7].word;
        // Dump memory around SP to inspect return address
        uint16_t mem_lo = read_word_le(sp_addr);
        uint16_t mem_hi = read_word_le(sp_addr + 2);
        printf("TRACE RET: SP=0x%04X MEM[SP]=0x%04X MEM[SP+2]=0x%04X\n", sp_addr, mem_lo, mem_hi);
        uint16_t return_address = read_word_le(sp_addr); // Pop return address (little-endian)
        cpu_instance.registers[7].word += 2;                                    // Move stack pointer (R7) up
        printf("      RETADDR=0x%04X SP(after pop)=0x%04X\n", return_address, cpu_instance.registers[7].word);
        cpu_instance.pc = return_address;                                       // Jump back to the return address
        break;
    }
    case 0x1C: // Load from base+offset LDROFF (4 byte I-type)
    {
        // Format: first word has opcode (5), dst (3), base (3), unused
        // Second word has signed 16-bit offset
        uint16_t offset_imm = read_immediate16_i4();
        int16_t signed_offset = static_cast<int16_t>(offset_imm);
        uint16_t address = cpu_instance.registers[register2].word + signed_offset;
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
        write_word_le(address, cpu_instance.registers[register1].word);
        cpu_instance.pc += 4;
        break;
    }
    case 0x1E: // Multiply register by register or immediate MUL (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::mul, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    case 0x1F: // Divide register by register or immediate DIV (R-type or 4 byte I-type)
    {
        execute_alu(alu_op::div, register1, register2, size_flag, lower_flag, mem_flag);
        break;
    }
    default:
        // Handle unknown opcode
        break;
    }
}

bool run_20k_instruction()
{
    int instruction_count = 0;
    
    // İlk instruction'ı logla
    uint16_t first = read_word_le(0);
    // Serial.printf("First instr: 0x%04X\n", first);
    // Serial.printf("mem[0]=0x%02X mem[1]=0x%02X\n", cpu_instance.memory[0], cpu_instance.memory[1]);
    
    while (cpu_instance.running && instruction_count < 100000)
    {
        uint16_t instruction = read_word_le(cpu_instance.pc);
        decode_and_execute(instruction);
        instruction_count++;
    }
    return cpu_instance.running;
}