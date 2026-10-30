# Python counterpart of the configurations in hardware/src/hdl, built on libstf's configuration
# utils. Keep the register encodings in sync with the hardware.
from libstf_utils.fpga_configuration import Config, RegisterAccess, REGISTER_BITS


class FilterCompareConfig(Config):
    """
    Configures the closed interval lo <= x <= hi of the FilterCompare of each decoder.
    """

    ID = 0x14e8418192d46a72 # FILTER_COMPARE_CONFIG_ID

    # Write registers per decoder: lo, hi and the control register
    NUM_DECODER_REGS = 3 # NUM_FILTER_COMPARE_CONFIG_REGS
    LO_REG      = 0
    HI_REG      = 1
    CONTROL_REG = 2

    # The control register packs (MSB -> LSB) as: negate [1 bit] | enable [1 bit]
    ENABLE_SHIFT = 0
    NEGATE_SHIFT = 1

    def __init__(self, registers: RegisterAccess, addr_offset: int, num_regs: int):
        super().__init__(registers, addr_offset, num_regs)
        self.num_decoders = self.read_register(1)

    def set_bounds(self, decoder: int, lo: int, hi: int, negate: bool = False):
        """
        Lets the values of the decoder pass if lo <= x <= hi (signed), or if not with negate. INT32
        columns compare against the low 32 bits of lo and hi.
        """
        assert decoder < self.num_decoders
        assert -pow(2, 63) <= lo < pow(2, 63) and -pow(2, 63) <= hi < pow(2, 63)

        mask = pow(2, REGISTER_BITS) - 1
        base = decoder * FilterCompareConfig.NUM_DECODER_REGS
        self.write_register(base + FilterCompareConfig.LO_REG, lo & mask)
        self.write_register(base + FilterCompareConfig.HI_REG, hi & mask)
        self.write_register(
            base + FilterCompareConfig.CONTROL_REG,
            int(negate) << FilterCompareConfig.NEGATE_SHIFT | 1 << FilterCompareConfig.ENABLE_SHIFT,
        )

    def disable(self, decoder: int):
        """
        Lets all values of the decoder pass.
        """
        assert decoder < self.num_decoders

        base = decoder * FilterCompareConfig.NUM_DECODER_REGS
        self.write_register(base + FilterCompareConfig.CONTROL_REG, 0)
