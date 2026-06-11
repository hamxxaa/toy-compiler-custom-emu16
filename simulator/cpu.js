/**
 * EMU16 CPU Emulator — JavaScript port of emu.cpp
 * 
 * 16-bit CPU with:
 *   - 8 registers (R0–R7), each 16-bit with upper/lower byte access
 *   - 64KB memory (0x0000–0xFFFF)
 *   - 16-bit PC, 16-bit flags (zero, sign, carry)
 *   - R7 = stack pointer, R6 = frame pointer (by convention)
 */

// Memory layout constants (from definitions.h)
const INPUT_ADDRESS      = 0xADFF;
const VRAM_START_ADDRESS = 0xB000;
const VRAM_SIZE          = 20480;  // 160×128
const PRAM_START_ADDRESS = 0xAE00;
const PRAM_SIZE          = 512;    // 256 colors × 2 bytes
const MAX_RAM_ADDRESS    = 0xFFFF;
const STACK_START_ADDRESS = 0xADFE;
const SCREEN_WIDTH       = 160;
const SCREEN_HEIGHT      = 128;

// ALU operation enum
const ALU_ADD     = 0;
const ALU_SUB     = 1;
const ALU_BIT_AND = 2;
const ALU_BIT_OR  = 3;
const ALU_BIT_XOR = 4;
const ALU_SHL     = 5;
const ALU_SHR     = 6;

class CPU {
    constructor() {
        // 64KB memory
        this.memory = new Uint8Array(65536);

        // 8 registers stored as 16-bit values
        this._registers = new Uint16Array(8);

        // Program counter
        this.pc = 0;

        // Flags: bit 0 = zero, bit 1 = sign, bit 2 = carry
        this.flags = 0;

        // Running state
        this.running = false;

        // Instruction counter (for debug)
        this.instructionCount = 0;
    }

    // --- Register access (simulates the C union) ---

    getRegWord(index) {
        return this._registers[index];
    }

    setRegWord(index, value) {
        this._registers[index] = value & 0xFFFF;
    }

    getRegLower(index) {
        return this._registers[index] & 0xFF;
    }

    setRegLower(index, value) {
        this._registers[index] = (this._registers[index] & 0xFF00) | (value & 0xFF);
    }

    getRegUpper(index) {
        return (this._registers[index] >> 8) & 0xFF;
    }

    setRegUpper(index, value) {
        this._registers[index] = (this._registers[index] & 0x00FF) | ((value & 0xFF) << 8);
    }

    // --- Memory access (little-endian) ---

    readWordLE(address) {
        return this.memory[address] | (this.memory[(address + 1) & 0xFFFF] << 8);
    }

    writeWordLE(address, value) {
        this.memory[address] = value & 0xFF;
        this.memory[(address + 1) & 0xFFFF] = (value >> 8) & 0xFF;
    }

    // --- Flag management ---

    setFlags(result) {
        this.flags = 0;
        if ((result & 0xFFFF) === 0)    this.flags |= 0x01;  // Zero flag
        if (result & 0x8000)            this.flags |= 0x02;  // Sign flag
        if (result > 0xFFFF)            this.flags |= 0x04;  // Carry flag
    }

    // --- Immediate read ---

    readImmediate16_i4() {
        return this.readWordLE((this.pc + 2) & 0xFFFF);
    }

    // --- ALU operations ---

    applyWordALU(lhs, rhs, op) {
        switch (op) {
            case ALU_ADD:     return (lhs + rhs) & 0x1FFFF;  // 17-bit to detect carry
            case ALU_SUB:     return ((lhs - rhs) + 0x20000) & 0x1FFFF;
            case ALU_BIT_AND: return lhs & rhs;
            case ALU_BIT_OR:  return lhs | rhs;
            case ALU_BIT_XOR: return lhs ^ rhs;
            case ALU_SHL:     return (lhs << (rhs & 0x0F)) & 0x1FFFF;
            case ALU_SHR:     return (lhs >>> (rhs & 0x0F));
            default:          return 0;
        }
    }

    applyByteALU(lhs, rhs, op) {
        switch (op) {
            case ALU_ADD:     return (lhs + rhs) & 0x1FF;
            case ALU_SUB:     return ((lhs - rhs) + 0x200) & 0x1FF;
            case ALU_BIT_AND: return lhs & rhs;
            case ALU_BIT_OR:  return lhs | rhs;
            case ALU_BIT_XOR: return lhs ^ rhs;
            case ALU_SHL:     return (lhs << (rhs & 0x07)) & 0x1FF;
            case ALU_SHR:     return (lhs >>> (rhs & 0x07));
            default:          return 0;
        }
    }

    executeALU(op, register1, register2, sizeFlag, lowerFlag) {
        let result;

        if (sizeFlag === 0) {
            // 4-byte I-type instruction
            const fullImmediate = this.readImmediate16_i4();
            result = this.applyWordALU(this.getRegWord(register1), fullImmediate, op);
            this.setRegWord(register1, result & 0xFFFF);
            this.setFlags(result);
            this.pc = (this.pc + 4) & 0xFFFF;
            return;
        }

        // R-type byte instruction
        if (lowerFlag === 0) {
            result = this.applyByteALU(this.getRegUpper(register1), this.getRegUpper(register2), op);
            this.setRegUpper(register1, result & 0xFF);
        } else {
            result = this.applyByteALU(this.getRegLower(register1), this.getRegLower(register2), op);
            this.setRegLower(register1, result & 0xFF);
        }

        this.setFlags(result);
        this.pc = (this.pc + 2) & 0xFFFF;
    }

    // --- CPU initialization ---

    initialize() {
        this.running = true;
        this.pc = 0;
        this.flags = 0;
        this.instructionCount = 0;

        for (let i = 0; i < 8; i++) {
            this._registers[i] = 0;
        }

        // Initialize stack pointer (R7)
        this._registers[7] = STACK_START_ADDRESS;

        // Clear all memory
        this.memory.fill(0);
    }

    // --- Instruction decode & execute ---

    decodeAndExecute(instruction) {
        const opcode    = (instruction >> 11) & 0x1F;
        const register1 = (instruction >> 8)  & 0x07;
        const register2 = (instruction >> 5)  & 0x07;
        const sizeFlag  = (instruction >> 4)  & 0x01;
        const lowerFlag = (instruction >> 3)  & 0x01;
        const immediate = instruction & 0xFF;

        switch (opcode) {
            case 0x00: // NOP
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x01: // HLT
                this.running = false;
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x02: // LDI_LO — load immediate to lower byte
                this.setRegLower(register1, immediate);
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x03: // LDI_HI — load immediate to upper byte
                this.setRegUpper(register1, immediate);
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x04: { // LDI — load 16-bit immediate to register
                const fullImm = this.readImmediate16_i4();
                this.setRegWord(register1, fullImm);
                this.pc = (this.pc + 4) & 0xFFFF;
                break;
            }

            case 0x05: { // STRI — store register to immediate memory address
                const address = this.readImmediate16_i4();
                this.memory[address] = this.getRegLower(register1);
                this.memory[(address + 1) & 0xFFFF] = this.getRegUpper(register1);
                this.pc = (this.pc + 4) & 0xFFFF;
                break;
            }

            case 0x06: { // STR — store register to register-pointed memory
                const addr = this.getRegWord(register2);
                if (sizeFlag === 0) {
                    // Word operation
                    this.memory[addr] = this.getRegLower(register1);
                    this.memory[(addr + 1) & 0xFFFF] = this.getRegUpper(register1);
                } else {
                    // Byte operation
                    if (lowerFlag === 0) {
                        this.memory[addr] = this.getRegUpper(register1);
                    } else {
                        this.memory[addr] = this.getRegLower(register1);
                    }
                }
                this.pc = (this.pc + 2) & 0xFFFF;
                break;
            }

            case 0x07: { // LDR — load memory to register
                const addr = this.getRegWord(register2);
                if (sizeFlag === 0) {
                    // Word operation
                    this.setRegLower(register1, this.memory[addr]);
                    this.setRegUpper(register1, this.memory[(addr + 1) & 0xFFFF]);
                } else {
                    // Byte operation
                    if (lowerFlag === 0) {
                        this.setRegUpper(register1, this.memory[addr]);
                    } else {
                        this.setRegLower(register1, this.memory[addr]);
                    }
                }
                this.pc = (this.pc + 2) & 0xFFFF;
                break;
            }

            case 0x08: // MOV — register to register move
                if (sizeFlag === 0) {
                    this.setRegWord(register1, this.getRegWord(register2));
                } else {
                    if (lowerFlag === 0) {
                        this.setRegUpper(register1, this.getRegUpper(register2));
                    } else {
                        this.setRegLower(register1, this.getRegLower(register2));
                    }
                }
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x09: // ADD
                this.executeALU(ALU_ADD, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x0A: // SUB
                this.executeALU(ALU_SUB, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x0B: // AND
                this.executeALU(ALU_BIT_AND, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x0C: // OR
                this.executeALU(ALU_BIT_OR, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x0D: // XOR
                this.executeALU(ALU_BIT_XOR, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x0E: // SHL
                this.executeALU(ALU_SHL, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x0F: // SHR
                this.executeALU(ALU_SHR, register1, register2, sizeFlag, lowerFlag);
                break;

            case 0x10: { // CMP — compare (subtract without storing)
                let result;
                if (sizeFlag === 0) {
                    const fullImm = this.readImmediate16_i4();
                    result = this.getRegWord(register1) - fullImm;
                    if (result < 0) result += 0x20000;
                    this.setFlags(result);
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                if (sizeFlag === 1) {
                    if (lowerFlag === 0) {
                        result = this.getRegUpper(register1) - this.getRegUpper(register2);
                    } else {
                        result = this.getRegLower(register1) - this.getRegLower(register2);
                    }
                    if (result < 0) result += 0x20000;
                    this.setFlags(result);
                    this.pc = (this.pc + 2) & 0xFFFF;
                }
                break;
            }

            case 0x11: // JZ — jump if zero flag set
                if (this.flags & 0x01) {
                    this.pc = this.readImmediate16_i4();
                } else {
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                break;

            case 0x12: // JNZ — jump if zero flag not set
                if (!(this.flags & 0x01)) {
                    this.pc = this.readImmediate16_i4();
                } else {
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                break;

            case 0x13: // JS — jump if sign flag set
                if (this.flags & 0x02) {
                    this.pc = this.readImmediate16_i4();
                } else {
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                break;

            case 0x14: // JNS — jump if sign flag not set
                if (!(this.flags & 0x02)) {
                    this.pc = this.readImmediate16_i4();
                } else {
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                break;

            case 0x15: // JC — jump if carry flag set
                if (this.flags & 0x04) {
                    this.pc = this.readImmediate16_i4();
                } else {
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                break;

            case 0x16: // JNC — jump if carry flag not set
                if (!(this.flags & 0x04)) {
                    this.pc = this.readImmediate16_i4();
                } else {
                    this.pc = (this.pc + 4) & 0xFFFF;
                }
                break;

            case 0x17: // JMP — unconditional jump
                this.pc = this.readImmediate16_i4();
                break;

            case 0x18: // PSH — push register to stack
                this._registers[7] = (this._registers[7] - 2) & 0xFFFF;
                this.writeWordLE(this._registers[7], this.getRegWord(register1));
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x19: // POP — pop register from stack
                this.setRegWord(register1, this.readWordLE(this._registers[7]));
                this._registers[7] = (this._registers[7] + 2) & 0xFFFF;
                this.pc = (this.pc + 2) & 0xFFFF;
                break;

            case 0x1A: { // CAL — call subroutine
                const target = this.readImmediate16_i4();
                this._registers[7] = (this._registers[7] - 2) & 0xFFFF;
                this.writeWordLE(this._registers[7], (this.pc + 4) & 0xFFFF);
                this.pc = target;
                break;
            }

            case 0x1B: { // RET — return from subroutine
                const returnAddr = this.readWordLE(this._registers[7]);
                this._registers[7] = (this._registers[7] + 2) & 0xFFFF;
                this.pc = returnAddr;
                break;
            }

            case 0x1C: { // LDROFF — load from base+offset
                const offset = this.readImmediate16_i4();
                const signedOffset = (offset & 0x8000) ? offset - 0x10000 : offset;
                const address = (this.getRegWord(register2) + signedOffset) & 0xFFFF;
                this.setRegWord(register1, this.readWordLE(address));
                this.pc = (this.pc + 4) & 0xFFFF;
                break;
            }

            case 0x1D: { // STROFF — store to base+offset
                const offset = this.readImmediate16_i4();
                const signedOffset = (offset & 0x8000) ? offset - 0x10000 : offset;
                const address = (this.getRegWord(register2) + signedOffset) & 0xFFFF;
                this.writeWordLE(address, this.getRegWord(register1));
                this.pc = (this.pc + 4) & 0xFFFF;
                break;
            }

            default:
                // Unknown opcode — skip
                this.pc = (this.pc + 2) & 0xFFFF;
                break;
        }
    }

    // --- Batch execution ---

    /**
     * Run up to `count` instructions. Returns true if CPU is still running.
     */
    runBatch(count = 100000) {
        let executed = 0;
        while (this.running && executed < count) {
            const instruction = this.readWordLE(this.pc);
            this.decodeAndExecute(instruction);
            executed++;
            this.instructionCount++;
        }
        return this.running;
    }

    /**
     * Execute a single instruction. Returns true if CPU is still running.
     */
    step() {
        if (!this.running) return false;
        const instruction = this.readWordLE(this.pc);
        this.decodeAndExecute(instruction);
        this.instructionCount++;
        return this.running;
    }
}

// Export for use in other modules
window.CPU = CPU;
window.EMU_CONSTANTS = {
    INPUT_ADDRESS,
    VRAM_START_ADDRESS,
    VRAM_SIZE,
    PRAM_START_ADDRESS,
    PRAM_SIZE,
    MAX_RAM_ADDRESS,
    STACK_START_ADDRESS,
    SCREEN_WIDTH,
    SCREEN_HEIGHT
};
