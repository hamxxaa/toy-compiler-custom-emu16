# 16-bit CPU ISA opcodes used by the emulator.

OP_NOP = 0x00
OP_HLT = 0x01
OP_LDI_LO = 0x02
OP_LDI_HI = 0x03
OP_LDI = 0x04
OP_STRI = 0x05
OP_STR = 0x06
OP_LDR = 0x07
OP_MOV = 0x08
OP_ADD = 0x09
OP_SUB = 0x0A
OP_AND = 0x0B
OP_OR = 0x0C
OP_XOR = 0x0D
OP_SHL = 0x0E
OP_SHR = 0x0F
OP_CMP = 0x10
OP_JZ = 0x11
OP_JNZ = 0x12
OP_JS = 0x13
OP_JNS = 0x14
OP_JC = 0x15
OP_JNC = 0x16
OP_JMP = 0x17
OP_PSH = 0x18
OP_POP = 0x19
OP_CAL = 0x1A
OP_RET = 0x1B
OP_LDROFF = 0x1C  # Load from base+offset
OP_STROFF = 0x1D  # Store to base+offset
OP_MUL = 0x1E
OP_DIV = 0x1F


def pack_r_type(opcode, reg1, reg2=0, size_flag=1, lower_flag=1):
    # Note: bit 2 is reserved for mem_flag (used in M-type instructions)
    return (
        ((opcode & 0x1F) << 11)
        | ((reg1 & 0x07) << 8)
        | ((reg2 & 0x07) << 5)
        | ((size_flag & 0x01) << 4)
        | ((lower_flag & 0x01) << 3)
    )

def pack_m_type(opcode, reg1, base_reg):
    # M-type: size_flag=0, lower_flag=1, mem_flag=1
    return (
        ((opcode & 0x1F) << 11)
        | ((reg1 & 0x07) << 8)
        | ((base_reg & 0x07) << 5)
        | (0 << 4) # size_flag
        | (1 << 3) # lower_flag
        | (1 << 2) # mem_flag
    )


ABS_FLAG = 1 << 1  # M-type / LDROFF absolute-addressing flag (bit 1)


def pack_m_type_abs(opcode, reg1):
    # Absolute M-type: base register ignored, abs flag set. The 16-bit immediate that
    # follows the header is the absolute address (not a base+offset).
    return pack_m_type(opcode, reg1, 0) | ABS_FLAG


def pack_i8(opcode, reg1, immediate8):
    return ((opcode & 0x1F) << 11) | ((reg1 & 0x07) << 8) | (immediate8 & 0xFF)


def pack_i16_header(opcode, reg1=0, size_flag=0, lower_flag=0):
    return (
        ((opcode & 0x1F) << 11)
        | ((reg1 & 0x07) << 8)
        | ((size_flag & 0x01) << 4)
        | ((lower_flag & 0x01) << 3)
    )
