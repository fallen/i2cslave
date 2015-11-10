#!/usr/bin/env python3
import argparse
import os
from fractions import Fraction

from migen.genlib.resetsync import AsyncResetSynchronizer
from migen.build.platforms import pipistrello
from migen.build.generic_platform import Pins, IOStandard

from misoc.interconnect.csr import *
from misoc.interconnect.wishbone import Converter, Interface
from misoc.cores.sdram_settings import MT46H32M16
from misoc.cores.sdram_phy import S6HalfRateDDRPHY
from misoc.cores import spi_flash
from misoc.cores.gpio import GPIOIn
from misoc.integration.soc_sdram import *
from misoc.integration.builder import *
from migen.fhdl.specials import Tristate

from ..platforms import pipistrello_i2c
from ..gateware.i2cslave import I2CSlave


i2cslave_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "..")


class Clock(Module, AutoCSR):
    def __init__(self, pad):
        self._r = CSRStatus(1)

        sample = Signal()
        ratio = int(83.3e6//100e3//2)
        counter = Signal(max=ratio)

        self.comb += pad.eq(sample)

        self.sync += [
            self._r.status.eq(sample),
            If(counter == ratio,
               counter.eq(0),
               sample.eq(~sample),
            ).Else(
               counter.eq(counter + 1),
            )
        ]


class GPIOInOut(Module, AutoCSR):
    def __init__(self, pad):
        self._w = CSRStorage(2)
        self._r = CSRStatus(1)

        self.value = Signal()
        self.oe = Signal()
        self.r = Signal()

        self.comb += [
            self.value.eq(self._w.storage[0]),
            self.oe.eq(self._w.storage[1]),
            self._r.status.eq(self.r),
        ]

        t = Tristate(pad, self.value, self.oe, self.r)
        self.specials += t


class _CRG(Module):
    def __init__(self, platform, clk_freq):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_sdram_half = ClockDomain()
        self.clock_domains.cd_sdram_full_wr = ClockDomain()
        self.clock_domains.cd_sdram_full_rd = ClockDomain()

        self.clk4x_wr_strb = Signal()
        self.clk4x_rd_strb = Signal()

        f0 = Fraction(50, 1)*1000000
        p = 12
        f = Fraction(clk_freq*p, f0)
        n, d = f.numerator, f.denominator
        assert 19e6 <= f0/d <= 500e6  # pfd
        assert 400e6 <= f0*n/d <= 1080e6  # vco

        clk50 = platform.request("clk50")
        clk50a = Signal()
        self.specials += Instance("IBUFG", i_I=clk50, o_O=clk50a)
        clk50b = Signal()
        self.specials += Instance("BUFIO2", p_DIVIDE=1,
                                  p_DIVIDE_BYPASS="TRUE", p_I_INVERT="FALSE",
                                  i_I=clk50a, o_DIVCLK=clk50b)
        pll_lckd = Signal()
        pll_fb = Signal()
        pll = Signal(6)
        self.specials.pll = Instance("PLL_ADV", p_SIM_DEVICE="SPARTAN6",
                                     p_BANDWIDTH="OPTIMIZED", p_COMPENSATION="INTERNAL",
                                     p_REF_JITTER=.01, p_CLK_FEEDBACK="CLKFBOUT",
                                     i_DADDR=0, i_DCLK=0, i_DEN=0, i_DI=0, i_DWE=0, i_RST=0, i_REL=0,
                                     p_DIVCLK_DIVIDE=d, p_CLKFBOUT_MULT=n, p_CLKFBOUT_PHASE=0.,
                                     i_CLKIN1=clk50b, i_CLKIN2=0, i_CLKINSEL=1,
                                     p_CLKIN1_PERIOD=1e9/f0, p_CLKIN2_PERIOD=0.,
                                     i_CLKFBIN=pll_fb, o_CLKFBOUT=pll_fb, o_LOCKED=pll_lckd,
                                     o_CLKOUT0=pll[0], p_CLKOUT0_DUTY_CYCLE=.5,
                                     o_CLKOUT1=pll[1], p_CLKOUT1_DUTY_CYCLE=.5,
                                     o_CLKOUT2=pll[2], p_CLKOUT2_DUTY_CYCLE=.5,
                                     o_CLKOUT3=pll[3], p_CLKOUT3_DUTY_CYCLE=.5,
                                     o_CLKOUT4=pll[4], p_CLKOUT4_DUTY_CYCLE=.5,
                                     o_CLKOUT5=pll[5], p_CLKOUT5_DUTY_CYCLE=.5,
                                     p_CLKOUT0_PHASE=0., p_CLKOUT0_DIVIDE=p//4,  # sdram wr rd
                                     p_CLKOUT1_PHASE=0., p_CLKOUT1_DIVIDE=p//4,
                                     p_CLKOUT2_PHASE=270., p_CLKOUT2_DIVIDE=p//2,  # sdram dqs adr ctrl
                                     p_CLKOUT3_PHASE=250., p_CLKOUT3_DIVIDE=p//2,  # off-chip ddr
                                     p_CLKOUT4_PHASE=0., p_CLKOUT4_DIVIDE=p//1,
                                     p_CLKOUT5_PHASE=0., p_CLKOUT5_DIVIDE=p//1,  # sys
        )
        self.specials += Instance("BUFG", i_I=pll[5], o_O=self.cd_sys.clk)
        reset = platform.request("user_btn")
        self.clock_domains.cd_por = ClockDomain()
        por = Signal(max=1 << 11, reset=(1 << 11) - 1)
        self.sync.por += If(por != 0, por.eq(por - 1))
        self.comb += self.cd_por.clk.eq(self.cd_sys.clk)
        self.specials += AsyncResetSynchronizer(self.cd_por, reset)
        self.specials += AsyncResetSynchronizer(self.cd_sys, ~pll_lckd | (por > 0))
        self.specials += Instance("BUFG", i_I=pll[2], o_O=self.cd_sdram_half.clk)
        self.specials += Instance("BUFPLL", p_DIVIDE=4,
                                  i_PLLIN=pll[0], i_GCLK=self.cd_sys.clk,
                                  i_LOCKED=pll_lckd, o_IOCLK=self.cd_sdram_full_wr.clk,
                                  o_SERDESSTROBE=self.clk4x_wr_strb)
        self.comb += [
            self.cd_sdram_full_rd.clk.eq(self.cd_sdram_full_wr.clk),
            self.clk4x_rd_strb.eq(self.clk4x_wr_strb),
        ]
        clk_sdram_half_shifted = Signal()
        self.specials += Instance("BUFG", i_I=pll[3], o_O=clk_sdram_half_shifted)
        clk = platform.request("ddram_clock")
        self.specials += Instance("ODDR2", p_DDR_ALIGNMENT="NONE",
                                  p_INIT=0, p_SRTYPE="SYNC",
                                  i_D0=1, i_D1=0, i_S=0, i_R=0, i_CE=1,
                                  i_C0=clk_sdram_half_shifted, i_C1=~clk_sdram_half_shifted,
                                  o_Q=clk.p)
        self.specials += Instance("ODDR2", p_DDR_ALIGNMENT="NONE",
                                  p_INIT=0, p_SRTYPE="SYNC",
                                  i_D0=0, i_D1=1, i_S=0, i_R=0, i_CE=1,
                                  i_C0=clk_sdram_half_shifted, i_C1=~clk_sdram_half_shifted,
                                  o_Q=clk.n)


class BaseSoC(SoCSDRAM):
    csr_map = {
        "spiflash": 16,
    }
    csr_map.update(SoCSDRAM.csr_map)

    def __init__(self, clk_freq=(83 + Fraction(1, 3))*1000*1000,
                 platform=pipistrello.Platform(), **kwargs):
        SoCSDRAM.__init__(self, platform, clk_freq,
                          cpu_reset_address=0x170000,  # 1.5 MB
                          **kwargs)

        self.submodules.crg = _CRG(platform, clk_freq)

        if not self.integrated_main_ram_size:
            sdram_module = MT46H32M16(self.clk_freq)
            self.submodules.ddrphy = S6HalfRateDDRPHY(platform.request("ddram"),
                                                      sdram_module.memtype,
                                                      rd_bitslip=1,
                                                      wr_bitslip=3,
                                                      dqs_ddr_alignment="C1")
            self.comb += [
                self.ddrphy.clk4x_wr_strb.eq(self.crg.clk4x_wr_strb),
                self.ddrphy.clk4x_rd_strb.eq(self.crg.clk4x_rd_strb),
            ]
            self.register_sdram(self.ddrphy, "minicon",
                                sdram_module.geom_settings, sdram_module.timing_settings)

        if not self.integrated_rom_size:
            self.submodules.spiflash = spi_flash.SpiFlash(platform.request("spiflash4x"),
                                                          dummy=10, div=4)
            self.add_constant("SPIFLASH_PAGE_SIZE", 256)
            self.add_constant("SPIFLASH_SECTOR_SIZE", 0x10000)
            self.flash_boot_address = 0x180000
            self.register_rom(self.spiflash.bus, 0x1000000)

papilio_adapter_io = [
    ("gpio_out", 0, Pins("C:14"), IOStandard("LVTTL")),
    ("clk100", 0, Pins("C:15"), IOStandard("LVTTL")),
]

class I2CSoC(BaseSoC):

    csr_map = {
        "gpio_inout": 17,
        "clock": 18,
    }
    csr_map.update(BaseSoC.csr_map)

    def __init__(self, **kwargs):
        BaseSoC.__init__(self, platform=pipistrello_i2c.Platform(), **kwargs)

        platform = self.platform
        platform.add_extension(papilio_adapter_io)
        self.submodules.gpio_inout = GPIOInOut(platform.request("gpio_out"))
        self.submodules.clock = Clock(platform.request("clk100"))


soc_pipistrello_args = soc_sdram_args
soc_pipistrello_argdict = soc_sdram_argdict


def main():
    parser = argparse.ArgumentParser(description="MiSoC port to the Pipistrello with I2C pins")
    builder_args(parser)
    soc_pipistrello_args(parser)
    args = parser.parse_args()

    soc = I2CSoC(**soc_pipistrello_argdict(args))
    builder = Builder(soc, **builder_argdict(args))
    builder.add_software_package("software", os.path.join(i2cslave_dir,
                                                          "software"))
    builder.build()


if __name__ == "__main__":
    main()
