from __future__ import annotations
from dataclasses import replace
from typing import (
    Callable,
    ClassVar,
    Dict,
    List,
    Optional,
    Set,
    Union,
)

from .error import DecompFailure
from .ir_pattern import IrMatch, IrPattern
from .options import Target
from .asm_file import BodyPart, Label
from .asm_instruction import (
    Argument,
    AsmAddressMode,
    AsmGlobalSymbol,
    AsmInstruction,
    AsmLiteral,
    AsmState,
    BinOp,
    JumpTarget,
    Macro,
    Register,
    get_jump_target,
)
from .instruction import (
    Instruction,
    InstructionMeta,
    Location,
    StackLocation,
)
from .asm_pattern import (
    AsmMatch,
    AsmMatcher,
    AsmPattern,
    Replacement,
    SimpleAsmPattern,
    make_pattern,
)
from .translate import (
    Abi,
    AbiArgSlot,
    AddressMode,
    Arch,
    ArgLoc,
    BinaryOp,
    Cast,
    ErrorExpr,
    ExprStmt,
    Expression,
    InstrArgs,
    InstrMap,
    Literal,
    NodeState,
    StmtInstrMap,
    StoreInstrMap,
    TernaryOp,
    UnaryOp,
    as_intish,
    as_sintish,
    as_u32,
    as_uintish,
    as_type,
    format_hex,
)
from .evaluate import (
    add_imm,
    carry_add_to,
    carry_sub_from,
    fn_op,
    fold_divmod,
    fold_mul_chains,
    handle_add,
    handle_add_double,
    handle_add_float,
    handle_add_real,
    handle_addi,
    handle_addis,
    handle_cmpnez,
    handle_convert,
    handle_load,
    handle_loadx,
    handle_or,
    handle_rlwimi,
    handle_rlwinm,
    handle_rlwnm,
    handle_shift_right,
    handle_xor,
    load_upper,
    make_store,
    make_storex,
    void_fn_op,
)
from .flow_graph import ArchFlowGraph, FlowGraph, InstrRef, RefSet
from .types import FunctionSignature, Type


class FcmpoCrorPattern(SimpleAsmPattern):
    """
    For floating point, `x <= y` and `x >= y` use `cror` to OR together the `cr0_eq`
    bit with either `cr0_lt` or `cr0_gt`. Instead of implementing `cror`, we detect
    this pattern and and directly compute the two registers.
    """

    pattern = make_pattern(
        "fcmpo $cr0, $x, $y",
        "cror 2, N, 2",
    )

    def replace(self, m: AsmMatch) -> Optional[Replacement]:
        fcmpo = m.body[0]
        assert isinstance(fcmpo, Instruction)
        if m.literals["N"] == 0:
            return Replacement(
                [AsmInstruction("fcmpo.lte.fictive", fcmpo.args)], len(m.body)
            )
        elif m.literals["N"] == 1:
            return Replacement(
                [AsmInstruction("fcmpo.gte.fictive", fcmpo.args)], len(m.body)
            )
        return None


class FcmpuCrorSoPattern(AsmPattern):
    """fcmpu crF; cror so,eq,gt/lt -> fcmpo.so.{gte,lte}.fictive.

    Same-block instructions that do not write the field may intervene."""

    def match(self, matcher: AsmMatcher) -> Optional[Replacement]:
        if matcher.index >= len(matcher.input):
            return None
        fcmpu = matcher.input[matcher.index]
        if not isinstance(fcmpu, Instruction) or fcmpu.mnemonic != "fcmpu":
            return None
        if len(fcmpu.args) < 3:
            return None
        field_reg = fcmpu.args[0]
        if not isinstance(field_reg, Register):
            return None
        name = field_reg.register_name
        if not (name.startswith("cr") and name[2:].isdigit()):
            return None
        field = int(name[2:])
        so_bit = 4 * field + 3
        eq_bit = 4 * field + 2
        gt_bit = 4 * field + 1
        lt_bit = 4 * field + 0
        field_bits = {
            Register(f"cr{field}_lt"),
            Register(f"cr{field}_gt"),
            Register(f"cr{field}_eq"),
            Register(f"cr{field}_so"),
        }

        intervening: List[BodyPart] = []
        index = matcher.index + 1
        while index < len(matcher.input):
            part = matcher.input[index]
            if isinstance(part, Label):
                # Basic block boundary.
                return None
            if not isinstance(part, Instruction):
                return None
            if part.mnemonic == "cror" and len(part.args) == 3:
                literals = [a for a in part.args if isinstance(a, AsmLiteral)]
                if len(literals) == 3:
                    d, e, g = (lit.value for lit in literals)
                    if d == so_bit and e == eq_bit:
                        if g == gt_bit:
                            mn = "fcmpo.so.gte.fictive"
                        elif g == lt_bit:
                            mn = "fcmpo.so.lte.fictive"
                        else:
                            return None
                        return Replacement(
                            [AsmInstruction(mn, fcmpu.args)] + intervening,
                            2 + len(intervening),
                        )
            if part.jump_target is not None or part.is_return:
                return None
            if any(reg in field_bits for reg in part.outputs + part.clobbers):
                return None
            intervening.append(part)
            index += 1
        return None


class MfcrPattern(SimpleAsmPattern):
    """mfcr + rlwinm bit-extract -> the extracted crF_bit register."""

    BIT_NAMES = ("lt", "gt", "eq", "so")

    pattern = make_pattern(
        "mfcr $x",
        "rlwinm $x, $x, N, 31, 31",
    )

    def replace(self, m: AsmMatch) -> Optional[Replacement]:
        x = m.regs["x"]
        p = (31 + m.literals["N"]) % 32
        field = p // 4
        bit = p % 4
        reg = Register(f"cr{field}_{self.BIT_NAMES[bit]}")
        return Replacement([AsmInstruction("move.fictive", [x, reg])], len(m.body))


class TailCallPattern(AsmPattern):
    """
    If a function ends in `return fn(...);` then the compiler may perform tail-call
    optimization. This is emitted as `b fn` instead of using `bl fn; blr`.
    """

    def match(self, matcher: AsmMatcher) -> Optional[Replacement]:
        if matcher.index != len(matcher.input) - 1:
            return None
        instr = matcher.input[matcher.index]
        if (
            isinstance(instr, Instruction)
            and instr.mnemonic == "b"
            and isinstance(instr.args[0], AsmGlobalSymbol)
            and not matcher.is_local_label(instr.args[0].symbol_name)
        ):
            return Replacement(
                [
                    AsmInstruction("bl", instr.args),
                    AsmInstruction("blr", []),
                ],
                1,
            )
        return None


class SaveRestoreRegsFnPattern(AsmPattern):
    """Expand calls to MWCC's built-in `_{save,rest}{gpr,fpr}_` functions into
    register saves/restores."""

    def match(self, matcher: AsmMatcher) -> Optional[Replacement]:
        bl = matcher.input[matcher.index]
        if (
            not isinstance(bl, Instruction)
            or bl.mnemonic != "bl"
            or not isinstance(bl.args[0], AsmGlobalSymbol)
        ):
            return None
        parts = bl.args[0].symbol_name.split("_")
        if len(parts) != 3 or parts[0]:
            return None
        if parts[1] in ("savegpr", "restgpr"):
            mnemonic = "stw" if parts[1] == "savegpr" else "lwz"
            size = 4
            reg_prefix = "r"
        elif parts[1] in ("savefpr", "restfpr"):
            mnemonic = "stfd" if parts[1] == "savefpr" else "lfd"
            size = 8
            reg_prefix = "f"
        else:
            return None

        # Find "addi $r11, $r1, N" above, with perhaps some instructions in between.
        for i in range(matcher.index - 1, -1, -1):
            instr = matcher.input[i]
            if (
                isinstance(instr, Instruction)
                and instr.mnemonic == "addi"
                and instr.args[0] == Register("r11")
                and instr.args[1] == Register("r1")
                and isinstance(instr.args[2], AsmLiteral)
            ):
                addend = instr.args[2].value
                break
        else:
            return None

        regnum = int(parts[2])
        new_instrs = []
        for i in range(regnum, 32):
            reg = Register(reg_prefix + str(i))
            stack_pos = AsmAddressMode(
                base=Register("r1"),
                addend=AsmLiteral(size * (i - 32) + addend),
                writeback=None,
            )
            new_instrs.append(AsmInstruction(mnemonic, [reg, stack_pos]))
        return Replacement(new_instrs, 1)


class CmpnezPattern1(IrPattern):
    """Comparison against 0. Sometimes a "neg" instruction gets added in front
    of this, for unclear reasons; handle_cmpnez takes care of removing it."""

    replacement = "cmpnez.fictive $o, $i"
    parts = [
        "addic $a, $i, -1",
        "subfe $o, $a, $i",
    ]


class CmpnezPattern2(IrPattern):
    replacement = "cmpnez.fictive $o, $i"
    parts = [
        "neg $a, $i",
        "or $b, $a, $i",
        "srwi $o, $b, 31",
    ]


class CmplezPattern(IrPattern):
    replacement = "cmplez.fictive $o, $i"
    parts = [
        "cntlzw $a, $i",
        "li $b, 1",
        "rlwnm $o, $b, $a, 31, 31",
    ]


class CmpgtzPattern(IrPattern):
    replacement = "cmpgtz.fictive $o, $i"
    parts = [
        "neg $a, $i",
        "andc $b, $a, $i",
        "srwi $o, $b, 31",
    ]


class CmpnePattern(IrPattern):
    replacement = "cmpne.fictive $o, $x, $y"
    parts = [
        "subf $a, $x, $y",
        "subf $b, $y, $x",
        "or $c, $a, $b",
        "srwi $o, $c, 31",
    ]


class CmplePattern(IrPattern):
    replacement = "cmple.fictive $o, $x, $y"
    parts = [
        "srawi $a, $y, 31",
        "srwi $b, $x, 31",
        "subfc $c, $x, $y",
        "adde $o, $a, $b",
    ]


class CmpltPattern1(IrPattern):
    replacement = "cmplt.fictive $o, $x, $y"
    parts = [
        "eqv $a, $y, $x",
        "subfc $b, $y, $x",
        "srwi $c, $a, 31",
        "addze $d, $c",
        "clrlwi $o, $d, 31",
    ]


class CmpltPattern2(IrPattern):
    replacement = "cmplt.fictive $o, $x, $y"
    parts = [
        "xor $a, $y, $x",
        "srawi $b, $a, 1",
        "and $c, $a, $y",
        "subf $d, $c, $b",
        "srwi $o, $d, 31",
    ]


class CmpleuPattern1(IrPattern):
    replacement = "cmpleu.fictive $o, $x, $y"
    parts = [
        "subf $a, $x, $y",
        "orc $b, $y, $x",
        "srwi $c, $a, 1",
        "subf $d, $c, $b",
        "srwi $o, $d, 31",
    ]


class CmpleuPattern2(IrPattern):
    replacement = "cmpleu.fictive $o, $x, $y"
    parts = [
        "li $a, -1",
        "subfc $b, $x, $y",
        "subfze $o, $a",
    ]


class CmpltuPattern1(IrPattern):
    replacement = "cmpltu.fictive $o, $x, $y"
    parts = [
        "subfc $a, $y, $x",
        "subfe $b, $a, $a",
        "neg $o, $b",
    ]


class CmpltuPattern2(IrPattern):
    replacement = "cmpltu.fictive $o, $x, $y"
    parts = [
        "xor $a, $y, $x",
        "cntlzw $b, $a",
        "slw $c, $y, $b",
        "srwi $o, $c, 31",
    ]


class BranchCtrPattern(AsmPattern):
    """Split decrement-$ctr-and-branch instructions into a pair of instructions."""

    def match(self, matcher: AsmMatcher) -> Optional[Replacement]:
        instr = matcher.input[matcher.index]
        if isinstance(instr, Instruction) and instr.mnemonic in ("bdz", "bdnz"):
            ctr = Register("ctr")
            return Replacement(
                [
                    AsmInstruction("addi", [ctr, ctr, AsmLiteral(-1)]),
                    AsmInstruction(instr.mnemonic + ".fictive", instr.args),
                ],
                1,
            )
        return None


class FloatishToUintPattern(SimpleAsmPattern):
    pattern = make_pattern("bl __cvt_fp2unsigned")

    def replace(self, m: AsmMatch) -> Optional[Replacement]:
        return Replacement(
            [AsmInstruction("cvt.u.d.fictive", [Register("r3"), Register("f1")])],
            len(m.body),
        )


class LoopStructCopyPattern(AsmPattern):
    loop_patterns = [
        # 4-aligned
        make_pattern(
            ".loop:",
            ".set W, 8",
            ".set B, 4",
            "lwz $a, 4($x)",
            "lwzu $b, 8($x)",
            "stw $a, 4($y)",
            "stwu $b, 8($y)",
            "bdnz .loop",
        ),
        make_pattern(
            ".loop:",
            ".set W, 8",
            ".set B, 8",
            "lwzu $a, 8($x)",
            "lwz $b, 4($x)",
            "stwu $a, 8($y)",
            "stw $b, 4($y)",
            "bdnz .loop",
        ),
        # 2-aligned
        make_pattern(
            ".loop:",
            ".set W, 4",
            ".set B, 2",
            "lhz $a, 2($x)",
            "lhzu $b, 4($x)",
            "sth $a, 2($y)",
            "sthu $b, 4($y)",
            "bdnz .loop",
        ),
        make_pattern(
            ".loop:",
            ".set W, 4",
            ".set B, 4",
            "lhzu $a, 4($x)",
            "lhz $b, 2($x)",
            "sthu $a, 4($y)",
            "sth $b, 2($y)",
            "bdnz .loop",
        ),
        # 1-aligned
        make_pattern(
            ".loop:",
            ".set W, 2",
            ".set B, 1",
            "lbz $a, 1($x)",
            "lbzu $b, 2($x)",
            "stb $a, 1($y)",
            "stbu $b, 2($y)",
            "bdnz .loop",
        ),
        make_pattern(
            ".loop:",
            ".set W, 2",
            ".set B, 2",
            "lbzu $a, 2($x)",
            "lbz $b, 1($x)",
            "stbu $a, 2($y)",
            "stb $b, 1($y)",
            "bdnz .loop",
        ),
    ]

    def match(self, matcher: AsmMatcher) -> Optional[Replacement]:
        for pattern in self.loop_patterns:
            m = matcher.try_match(pattern)
            if m is not None:
                break
        else:
            return None

        new_instr = AsmInstruction(
            "loopstructcopy.fictive",
            [
                m.regs["y"],
                m.regs["x"],
                m.regs["y"],
                m.regs["x"],
                Register("ctr"),
                AsmLiteral(m.literals["W"]),
                AsmLiteral(m.literals["B"]),
                AsmLiteral(0),
                AsmLiteral(0),
            ],
        )
        return Replacement([m.body[0], new_instr], len(m.body))


class StructCopyPattern(AsmPattern):
    """Recognizing struct copy when it starts with lwz lwz stw stw. Others
    would cause false positives. Maybe we can find another way for those using
    context?
    This pattern appears on almost every GC and Wii MW compiler version when using C
    and GC MW 1.0-1.2.5n when using C++.
    """

    pattern = make_pattern(
        "lwz $a, I($s)",
        "lwz $b, (I+4)($s)",
        "stw $a, I($d)",
        "stw $b, (I+4)($d)",
    )

    def match(self, matcher: AsmMatcher) -> Optional[Replacement]:
        # Use the initial patterns first
        m = matcher.try_match(self.pattern)
        if m is None:
            return None
        i = 8
        pattern_ext = self.pattern.copy()
        while True:
            pattern2 = make_pattern(
                f"lwz $a, (I+{i})($s)",
                f"lwz $b, (I+{i+4})($s)",
                f"stw $a, (I+{i})($d)",
                f"stw $b, (I+{i+4})($d)",
            )

            m2 = matcher.try_match(pattern_ext + pattern2)
            if m2:
                m = m2
                i += 8
                pattern_ext.extend(pattern2)
            else:
                break

        pattern_end_4b = make_pattern(
            f"lwz $b, (I+{i})($s)",
            f"stw $b, (I+{i})($d)",
        )
        m_end = matcher.try_match(pattern_ext + pattern_end_4b)
        if m_end:
            m = m_end
            i += 4
            pattern_ext.extend(pattern_end_4b)

        pattern_end_2b = make_pattern(
            f"lhz $b, (I+{i})($s)",
            f"sth $b, (I+{i})($d)",
        )
        m_end = matcher.try_match(pattern_ext + pattern_end_2b)
        if m_end:
            m = m_end
            i += 2
            pattern_ext.extend(pattern_end_2b)

        pattern_end_1b = make_pattern(
            f"lbz $b, (I+{i})($s)",
            f"stb $b, (I+{i})($d)",
        )
        m_end = matcher.try_match(pattern_ext + pattern_end_1b)
        if m_end:
            m = m_end
            i += 1

        return Replacement(
            [
                AsmInstruction(
                    "structcopy.fictive", [m.regs["d"], m.regs["s"], AsmLiteral(i)]
                )
            ],
            len(m.body),
        )


class FloatishToSintIrPattern(IrPattern):
    # This pattern handles converting either f32 or f64 into a signed int
    # The `fctiwz` instruction does all the work; this pattern is just to
    # elide the stack store/load pair.
    replacement = "fctiwz.fictive $i, $f"
    parts = [
        "fctiwz $t, $f",
        "stfd $t, (N-4)($r1)",
        "lwz $i, N($r1)",
    ]


class CheckConstantMixin:
    def check(self, m: IrMatch, arch: ArchFlowGraph, flow_graph: FlowGraph) -> bool:
        # TODO: Also validate that `K($k)` is the expected constant in rodata
        return m.symbolic_registers["k"] in (Register("r2"), Register("r13"))


class SintToDoubleIrPattern(IrPattern, CheckConstantMixin):
    # The replacement asm for these patterns reference the float constant `K($k)`
    # as an input, even though the value is ignored. This is needed to mark `$k`
    # as an input to the pattern for matching.
    replacement = "cvt.d.i.fictive $f, $i, K($k)"
    parts = [
        "lis $a, 0x4330",
        "stw $a, N($r1)",
        "xoris $b, $i, 0x8000",
        "stw $b, (N+4)($r1)",
        "lfd $d, N($r1)",
        "lfd $c, K($k)",
        "fsub $f, $d, $c",
    ]


class UintToDoubleIrPattern(IrPattern, CheckConstantMixin):
    replacement = "cvt.d.u.fictive $f, $i, K($k)"
    parts = [
        "lis $a, 0x4330",
        "stw $a, N($r1)",
        "stw $i, (N+4)($r1)",
        "lfd $d, N($r1)",
        "lfd $c, K($k)",
        "fsub $f, $d, $c",
    ]


class SintToFloatIrPattern(IrPattern, CheckConstantMixin):
    replacement = "cvt.s.i.fictive $f, $i, K($k)"
    parts = [
        "lis $a, 0x4330",
        "stw $a, N($r1)",
        "xoris $b, $i, 0x8000",
        "stw $b, (N+4)($r1)",
        "lfd $d, N($r1)",
        "lfd $c, K($k)",
        "fsubs $f, $d, $c",
    ]


class UintToFloatIrPattern(IrPattern, CheckConstantMixin):
    replacement = "cvt.s.u.fictive $f, $i, K($k)"
    parts = [
        "lis $a, 0x4330",
        "stw $a, N($r1)",
        "stw $i, (N+4)($r1)",
        "lfd $d, N($r1)",
        "lfd $c, K($k)",
        "fsubs $f, $d, $c",
    ]


class LoopStructCopySetupPattern(IrPattern):
    replacement = "loopstructcopy.fictive $a, $b, $x, $y, $z, W, B, B, T"
    parts = [
        "addi $a, $x, -B",
        "addi $b, $y, -B",
        "loopstructcopy.fictive $a, $b, $a, $b, $z, W, B, 0, 0",
    ]

    @staticmethod
    def _by_offset(refs: RefSet) -> Dict[int, InstrRef]:
        ret = {}
        for ref in refs:
            if not isinstance(ref, InstrRef):
                continue
            ins = ref.instruction
            if len(ins.args) != 2:
                continue
            arg = ins.args[1]
            if not isinstance(arg, AsmAddressMode):
                continue
            if not isinstance(arg.addend, AsmLiteral):
                continue
            ret[arg.addend.value] = ref
        return ret

    def check(self, m: IrMatch, arch: ArchFlowGraph, flow_graph: FlowGraph) -> bool:
        # Extend the pattern with an optional lwz+stw/lhz+sth/lbz+stb tail part,
        # marking those instructions as consumed and not to be emitted.
        offset = m.symbolic_args["B"]
        assert isinstance(offset, AsmLiteral)
        offset = offset.value
        tail_size = 0

        ref = m.body[2]
        assert ref.instruction.mnemonic == "loopstructcopy.fictive"
        dst = m.symbolic_registers["a"]
        src = m.symbolic_registers["b"]
        dst_refs_by_offset = self._by_offset(flow_graph.instr_uses[ref].get(dst))
        src_refs_by_offset = self._by_offset(flow_graph.instr_uses[ref].get(src))

        tails = [("lwz", "stw", 4), ("lhz", "sth", 2), ("lbz", "stb", 1)]
        for load_mn, store_mn, width in tails:
            load = src_refs_by_offset.get(offset + tail_size)
            store = dst_refs_by_offset.get(offset + tail_size)
            if (
                load is None
                or store is None
                or load.instruction.mnemonic != load_mn
                or store.instruction.mnemonic != store_mn
            ):
                continue
            temp_reg = load.instruction.args[0]
            assert isinstance(temp_reg, Register)
            if store not in flow_graph.instr_uses[load].get(temp_reg):
                continue
            tail_size += width
            load.instruction = replace(load.instruction, in_pattern=True)
            store.replace_instruction(AsmInstruction("nop", []), arch)

        m.symbolic_args["T"] = AsmLiteral(tail_size)
        return True


class PpcArch(Arch):
    arch = Target.ArchEnum.PPC

    re_comment = r"[#;].*"
    supports_dollar_regs = True

    home_space_size = 8

    stack_pointer_reg = Register("r1")
    frame_pointer_regs = [Register("r30")]
    return_address_reg = Register("lr")

    # Gekko SPR numbers. TBL/TBU read via mftb.
    spr_names = {
        "XER": 1, "LR": 8, "CTR": 9, "DSISR": 18, "DAR": 19, "DEC": 22,
        "SDR1": 25, "SRR0": 26, "SRR1": 27,
        "SPRG0": 272, "SPRG1": 273, "SPRG2": 274, "SPRG3": 275,
        "EAR": 282, "TBL": 284, "TBU": 285, "PVR": 287,
        "IBAT0U": 528, "IBAT0L": 529, "IBAT1U": 530, "IBAT1L": 531,
        "IBAT2U": 532, "IBAT2L": 533, "IBAT3U": 534, "IBAT3L": 535,
        "DBAT0U": 536, "DBAT0L": 537, "DBAT1U": 538, "DBAT1L": 539,
        "DBAT2U": 540, "DBAT2L": 541, "DBAT3U": 542, "DBAT3L": 543,
        "GQR0": 912, "GQR1": 913, "GQR2": 914, "GQR3": 915, "GQR4": 916,
        "GQR5": 917, "GQR6": 918, "GQR7": 919,
        "HID2": 920, "WPAR": 921, "DMA_U": 922, "DMA_L": 923,
        "UMMCR0": 936, "UPMC1": 937, "UPMC2": 938, "USIA": 939,
        "UMMCR1": 940, "UPMC3": 941, "UPMC4": 942, "USDA": 943,
        "MMCR0": 952, "PMC1": 953, "PMC2": 954, "SIA": 955, "MMCR1": 956,
        "PMC3": 957, "PMC4": 958, "SDA": 959,
        "HID0": 1008, "HID1": 1009, "IABR": 1010, "DABR": 1013,
        "L2CR": 1017, "ICTC": 1019, "THRM1": 1020, "THRM2": 1021,
        "THRM3": 1022,
    }

    @classmethod
    def _spr_number(cls, arg: Argument) -> Optional[int]:
        """Resolve an mfspr/mtspr SPR operand (literal number or name)."""
        if isinstance(arg, AsmLiteral):
            return arg.value
        if isinstance(arg, AsmGlobalSymbol):
            return cls.spr_names.get(arg.symbol_name.upper())
        return None

    base_return_regs = [(Register("r3"), False), (Register("f1"), True)]
    all_return_regs = [Register(r) for r in ["f1", "r3", "r4"]]
    argument_regs = [
        Register(r)
        for r in [
            "r3",
            "r4",
            "r5",
            "r6",
            "r7",
            "r8",
            "r9",
            "r10",
            "f1",
            "f2",
            "f3",
            "f4",
            "f5",
            "f6",
            "f7",
            "f8",
            "f9",
            "f10",
            "f11",
            "f12",
            "f13",
        ]
    ]
    simple_temp_regs = [Register(r) for r in ["r11", "r12"]]
    temp_regs = (
        argument_regs
        + simple_temp_regs
        + [
            Register(r)
            for r in [
                "r0",
                "f0",
                "cr0_gt",
                "cr0_lt",
                "cr0_eq",
                "cr0_so",
                "ctr",
            ]
        ]
    )
    saved_regs = [
        Register(r)
        for r in [
            # TODO: Some of the bits in CR are required to be saved (like cr2_gt)
            # When those bits are implemented, they should be added here
            "lr",
            # $r2 & $r13 are used for the small-data region, and are like $gp in MIPS
            "r2",
            "r13",
            "r14",
            "r15",
            "r16",
            "r17",
            "r18",
            "r19",
            "r20",
            "r21",
            "r22",
            "r23",
            "r24",
            "r25",
            "r26",
            "r27",
            "r28",
            "r29",
            "r30",
            "r31",
            "f14",
            "f15",
            "f16",
            "f17",
            "f18",
            "f19",
            "f20",
            "f21",
            "f22",
            "f23",
            "f24",
            "f25",
            "f26",
            "f27",
            "f28",
            "f29",
            "f30",
            "f31",
        ]
    ]
    all_regs = (
        saved_regs
        + temp_regs
        + [stack_pointer_reg]
        + [
            Register(r)
            for r in [
                # `zero` isn't a "real" PPC register; it's a normalized form of `r0`
                "zero",
                # CR field bit registers (crN_lt/gt/eq/so).
                "cr0",
                "cr1",
                "cr2",
                "cr3",
                "cr4",
                "cr5",
                "cr6",
                "cr7",
            ]
        ]
        + [
            Register(f"cr{n}_{bit}")
            for n in range(8)
            for bit in ("lt", "gt", "eq", "so")
        ]
    )

    aliased_regs: Dict[str, Register] = {}

    @classmethod
    def missing_return(cls) -> List[Instruction]:
        return [cls.parse("blr", [], InstructionMeta.missing())]

    # List of all instructions where `$r0` as certain args is interpreted as `0`
    # instead of the contents of `$r0`. The dict value represents the argument
    # index that is affected.
    INSTRS_R0_AS_ZERO: ClassVar[Dict[str, int]] = {
        "addi": 1,
        "addis": 1,
        "dcbf": 0,
        "dcbi": 0,
        "dcbst": 0,
        "dcbt": 0,
        "dcbtst": 0,
        "dcbz": 0,
        "dcbz_l": 0,
        "eciwx": 1,
        "ecowx": 1,
        "icbi": 0,
        "lbz": 1,
        "lbzx": 1,
        "lfd": 1,
        "lfdx": 1,
        "lfs": 1,
        "lfsx": 1,
        "lha": 1,
        "lhax": 1,
        "lhbrx": 1,
        "lhz": 1,
        "lhzx": 1,
        "lmw": 1,
        "lswi": 1,
        "lswx": 1,
        "lwarx": 1,
        "lwbrx": 1,
        "lwz": 1,
        "lwzx": 1,
        "psq_lx": 1,
        "psq_stx": 1,
        "stb": 1,
        "stbx": 1,
        "stfd": 1,
        "stfdx": 1,
        "stfiwx": 1,
        "stfs": 1,
        "stfsx": 1,
        "sth": 1,
        "sthbrx": 1,
        "sthx": 1,
        "stmw": 1,
        "stswi": 1,
        "stswx": 1,
        "stw": 1,
        "stwbrx": 1,
        "stwcx.": 1,
        "stwx": 1,
    }

    @classmethod
    def normalize_instruction(
        cls, instr: AsmInstruction, asm_state: AsmState
    ) -> AsmInstruction:
        # Remove +/- suffix, which indicates branch-(un)likely and can be ignored
        if instr.mnemonic.startswith("b") and (
            instr.mnemonic.endswith("+") or instr.mnemonic.endswith("-")
        ):
            return PpcArch.normalize_instruction(
                AsmInstruction(instr.mnemonic[:-1], instr.args),
                asm_state,
            )

        args = instr.args
        base_mnemonic = instr.mnemonic.rstrip(".")
        dot = instr.mnemonic[len(base_mnemonic) :]

        def sub(a: Argument, b: Argument) -> Argument:
            if isinstance(a, AsmLiteral) and isinstance(b, AsmLiteral):
                return AsmLiteral(a.value - b.value)
            else:
                return BinOp("-", a, b)

        def add(a: Argument, b: Argument) -> Argument:
            if isinstance(a, AsmLiteral) and isinstance(b, AsmLiteral):
                return AsmLiteral(a.value + b.value)
            else:
                return BinOp("+", a, b)

        def make_dotted(mn: str, args: List[Argument]) -> AsmInstruction:
            return AsmInstruction(mn + dot, args)

        lit = AsmLiteral

        r0_index = cls.INSTRS_R0_AS_ZERO.get(instr.mnemonic)
        if r0_index is not None and len(args) > r0_index:
            # If the argument at the given index is $r0, replace it with $zero
            r0_arg = args[r0_index]
            if r0_arg == Register("r0"):
                r0_arg = Register("zero")
            elif isinstance(r0_arg, AsmAddressMode) and r0_arg.base == Register("r0"):
                r0_arg = replace(r0_arg, base=Register("zero"))

            if r0_arg is not args[r0_index]:
                new_args = args[:]
                new_args[r0_index] = r0_arg
                return PpcArch.normalize_instruction(
                    AsmInstruction(instr.mnemonic, new_args),
                    asm_state,
                )
        if len(args) == 4:
            if base_mnemonic == "extlwi":
                return make_dotted(
                    "rlwinm", args[:2] + [args[3], lit(0), sub(args[2], lit(1))]
                )
            if base_mnemonic == "extrwi":
                return make_dotted(
                    "rlwinm",
                    args[:2] + [add(args[2], args[3]), sub(lit(32), args[2]), lit(31)],
                )
            if base_mnemonic == "clrlslwi":
                b = args[2]
                n = args[3]
                return make_dotted("rlwinm", args[:2] + [n, sub(b, n), sub(lit(31), n)])
        if len(args) == 3:
            if (
                instr.mnemonic == "addi"
                and isinstance(args[2], Macro)
                and args[1] in (Register("r2"), Register("r13"))
                and args[2].macro_name in ("sda2", "sda21")
            ):
                return AsmInstruction("li", [args[0], args[2].argument])
            if base_mnemonic in ("subi", "subis", "subic") and isinstance(
                args[2], AsmLiteral
            ):
                mn = "add" + base_mnemonic[3:]
                negated = AsmLiteral(-args[2].value)
                return cls.normalize_instruction(
                    make_dotted(mn, args[:2] + [negated]), asm_state
                )
            if base_mnemonic in ("sub", "subo", "subc", "subco"):
                mn = "subf" + base_mnemonic[3:]
                return make_dotted(mn, [args[0], args[2], args[1]])
            if base_mnemonic == "rotlwi":
                return make_dotted("rlwinm", args[:2] + [args[2], lit(0), lit(31)])
            if base_mnemonic == "rotlw":
                # Rotate left by a variable amount; rlwnm full mask.
                return make_dotted(
                    "rlwnm", args[:2] + [args[2], lit(0), lit(31)]
                )
            if base_mnemonic == "rotrwi":
                return make_dotted(
                    "rlwinm", args[:2] + [sub(lit(32), args[2]), lit(0), lit(31)]
                )
            if base_mnemonic == "slwi":
                return make_dotted(
                    "rlwinm", args[:2] + [args[2], lit(0), sub(lit(31), args[2])]
                )
            if base_mnemonic == "srwi":
                return make_dotted(
                    "rlwinm", args[:2] + [sub(lit(32), args[2]), args[2], lit(31)]
                )
            if base_mnemonic == "clrlwi":
                return make_dotted("rlwinm", args[:2] + [lit(0), args[2], lit(31)])
            if base_mnemonic == "clrrwi":
                return make_dotted(
                    "rlwinm", args[:2] + [lit(0), lit(0), sub(lit(31), args[2])]
                )
        if len(args) == 2:
            if instr.mnemonic == "lis" and isinstance(args[1], AsmLiteral):
                val = lit((args[1].value & 0xFFFF) << 16)
                return AsmInstruction("li", [args[0], val])
            if (
                instr.mnemonic == "lis"
                and isinstance(args[1], Macro)
                and args[1].macro_name == "ha"
                and isinstance(args[1].argument, AsmLiteral)
            ):
                # The @ha macro compensates for the sign bit of the corresponding @l
                value = args[1].argument.value
                if value & 0x8000:
                    value += 0x10000
                val = lit(value & 0xFFFF0000)
                return AsmInstruction("li", [args[0], val])
            if instr.mnemonic.startswith("cmp") and "fictive" not in instr.mnemonic:
                # For the two-argument form of cmpw, the insert an implicit CR0 as the first arg
                cr0: Argument = Register("cr0")
                return AsmInstruction(instr.mnemonic, [cr0] + instr.args)
        if instr.mnemonic in (
            "cror", "crnot", "crand", "crxor", "crnand", "crnor",
            "creqv", "crandc", "crorc",
        ):
            # Normalize named CR bits (un/lt/gt/eq/so) to numeric indices.
            cr_bit_names = {"lt": 0, "gt": 1, "eq": 2, "so": 3, "un": 3}

            def cr_bit_number(arg: Argument) -> Optional[Argument]:
                if not isinstance(arg, AsmGlobalSymbol):
                    return None
                name = arg.symbol_name.lower()
                if name in cr_bit_names:
                    return AsmLiteral(cr_bit_names[name])
                if name.startswith("cr") and len(name) > 2:
                    field = name[2:-2]
                    bit = name[-2:]
                    if field.isdigit() and bit in cr_bit_names:
                        return AsmLiteral(4 * int(field) + cr_bit_names[bit])
                return None

            new_args = []
            for arg in args:
                num = cr_bit_number(arg)
                new_args.append(num if num is not None else arg)
            if new_args != args:
                return AsmInstruction(instr.mnemonic, new_args)
        return instr

    @classmethod
    def parse(
        cls, mnemonic: str, args: List[Argument], meta: InstructionMeta
    ) -> Instruction:
        inputs: List[Location] = []
        clobbers: List[Location] = []
        outputs: List[Location] = []
        jump_target: Optional[Union[JumpTarget, Register]] = None
        function_target: Optional[Argument] = None
        is_conditional = False
        is_return = False
        is_load = False
        is_store = False
        eval_fn: Optional[Callable[[NodeState, InstrArgs], object]] = None

        instr_str = str(AsmInstruction(mnemonic, args))

        if mnemonic in ("cror", "crnot"):
            # cror D,E,G sets D = E|G; crnot D,S sets D = !S.
            literals = [a for a in args if isinstance(a, AsmLiteral)]
            bit_names = {0: "lt", 1: "gt", 2: "eq", 3: "so"}
            if mnemonic == "cror" and len(args) == 3 and len(literals) == 3:
                d, e, g = (lit.value for lit in literals)
                if d % 4 == 3 and e % 4 == 2 and d // 4 == e // 4 == g // 4:
                    if g % 4 in (0, 1):
                        field = d // 4
                        bit_name = "lt" if g % 4 == 0 else "gt"
                        eq_reg = Register(f"cr{field}_eq")
                        other_reg = Register(f"cr{field}_{bit_name}")
                        so_reg = Register(f"cr{field}_so")

                        def eval_cror(s: NodeState, a: InstrArgs) -> None:
                            eq = a.cmp_reg(eq_reg.register_name)
                            other = a.cmp_reg(other_reg.register_name)
                            combined = BinaryOp(
                                eq, "||", other, type=Type.boolean()
                            )
                            s.set_reg(so_reg, combined)

                        return Instruction(
                            mnemonic=mnemonic,
                            args=args,
                            meta=meta,
                            inputs=[eq_reg, other_reg],
                            clobbers=[],
                            outputs=[so_reg],
                            jump_target=None,
                            function_target=None,
                            is_conditional=False,
                            is_return=False,
                            is_load=False,
                            is_store=False,
                            eval_fn=eval_cror,
                        )
            if mnemonic == "crnot" and len(args) == 2 and len(literals) == 2:
                d, s = (lit.value for lit in literals)
                if d % 4 in bit_names and s % 4 in bit_names:
                    d_reg = Register(
                        f"cr{d // 4}_{bit_names[d % 4]}"
                    )
                    s_reg = Register(
                        f"cr{s // 4}_{bit_names[s % 4]}"
                    )

                    def eval_crnot(s: NodeState, a: InstrArgs) -> None:
                        val = a.cmp_reg(s_reg.register_name)
                        s.set_reg(
                            d_reg, UnaryOp("!", val, type=Type.boolean())
                        )

                    return Instruction(
                        mnemonic=mnemonic,
                        args=args,
                        meta=meta,
                        inputs=[s_reg],
                        clobbers=[],
                        outputs=[d_reg],
                        jump_target=None,
                        function_target=None,
                        is_conditional=False,
                        is_return=False,
                        is_load=False,
                        is_store=False,
                        eval_fn=eval_crnot,
                    )

        cr0_bits: List[Location] = [
            Register("cr0_lt"),
            Register("cr0_gt"),
            Register("cr0_eq"),
            Register("cr0_so"),
        ]

        def cr_field_bits(cr_field: Register) -> List[Register]:
            # The 4 bits (lt, gt, eq, so) of a condition register field.
            name = cr_field.register_name
            assert name.startswith("cr") and name[2:].isdigit()
            n = int(name[2:])
            return [
                Register(f"cr{n}_lt"),
                Register(f"cr{n}_gt"),
                Register(f"cr{n}_eq"),
                Register(f"cr{n}_so"),
            ]

        memory_sizes = {
            "b": 1,
            "h": 2,
            "w": 4,
            "fs": 4,
            "fd": 8,
        }
        psq_imms = 0
        size = memory_sizes.get(mnemonic.lstrip("stl").rstrip("azux"))
        if mnemonic.startswith("psq_l") or mnemonic.startswith("psq_st"):
            psq_imms = 2
            size = 8

        def make_memory_access(arg: Argument, size: int) -> List[Location]:
            assert size is not None
            if isinstance(arg, AsmAddressMode) and arg.base == cls.stack_pointer_reg:
                loc = StackLocation.from_offset(arg.addend)
                if loc is None:
                    return []
                elif size == 8:
                    return [loc, replace(loc, offset=loc.offset + 4)]
                else:
                    assert size <= 4
                    return [loc]
            return []

        def unreachable_eval(s: NodeState, a: InstrArgs) -> None:
            raise DecompFailure(
                f"Instruction {instr_str} should be replaced before eval"
            )

        if mnemonic == "blr":
            # Return
            assert len(args) == 0
            inputs = [Register("lr")]
            is_return = True
        elif mnemonic in (
            "beqlr",
            "bgelr",
            "bgtlr",
            "blelr",
            "bltlr",
            "bnelr",
            "bnglr",
            "bnllr",
            "bnslr",
            "bsolr",
        ):
            # Conditional return
            # TODO: Support crN argument
            assert len(args) <= 1
            inputs = cr0_bits + [Register("lr")]
            is_return = True
            is_conditional = True
            # NB: These are rewritten to mnemonic[:-2] by build_blocks in flow_graph.py
            eval_fn = unreachable_eval
        elif mnemonic == "bctr":
            # Jump table (switch)
            assert len(args) == 0
            inputs = [Register("ctr")]
            jump_target = Register("ctr")
            is_conditional = True
            eval_fn = lambda s, a: s.set_switch_expr(a.regs[Register("ctr")])
        elif mnemonic == "bl":
            # Function call to label
            assert len(args) == 1
            inputs = list(cls.argument_regs)
            outputs = list(cls.all_return_regs)
            clobbers = list(cls.temp_regs)
            function_target = args[0]
            eval_fn = lambda s, a: s.make_function_call(a.sym_imm(0), outputs)
        elif mnemonic in ("bctrl", "blrl"):
            # Function call to pointer in special reg ($ctr or $lr)
            assert len(args) == 0
            reg = Register(mnemonic[1:-1])
            inputs = list(cls.argument_regs)
            inputs.append(reg)
            outputs = list(cls.all_return_regs)
            clobbers = list(cls.temp_regs)
            function_target = reg
            eval_fn = lambda s, a: s.make_function_call(a.regs[reg], outputs)
        elif mnemonic == "b":
            # Unconditional jump
            assert len(args) == 1
            jump_target = get_jump_target(args[0])
        elif mnemonic in cls.instrs_branches:
            # Branch on a CR bit; a crN field arg overrides cr0.
            assert 1 <= len(args) <= 2
            raw_name = cls.instrs_branches[mnemonic]
            negated = raw_name.startswith("!")
            reg_name = raw_name.lstrip("!")
            if len(args) == 2 and isinstance(args[0], Register):
                cr_field = args[0].register_name
                if cr_field.startswith("cr") and cr_field[2:].isdigit():
                    suffix = reg_name.split("_", 1)[1]
                    reg_name = f"cr{cr_field[2:]}_{suffix}"

            inputs = [Register(reg_name)]
            jump_target = get_jump_target(args[-1])
            is_conditional = True

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                cond = a.cmp_reg(reg_name)
                if negated:
                    cond = cond.negated()
                s.set_branch_condition(cond)

        elif mnemonic in cls.instrs_store:
            assert isinstance(args[0], Register) and size is not None
            is_store = True
            if mnemonic.endswith("x"):
                assert (
                    len(args) == 3 + psq_imms
                    and isinstance(args[1], Register)
                    and isinstance(args[2], Register)
                )
                inputs = [args[0], args[1], args[2]]
            else:
                assert len(args) == 2 + psq_imms and isinstance(args[1], AsmAddressMode)
                outputs = make_memory_access(args[1], size)
                inputs = [args[0]] * (len(outputs) or 1)
                inputs.append(args[1].base)

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                store = cls.instrs_store[mnemonic](a)
                if store is not None:
                    s.store_memory(store, a.reg_ref(0))

        elif mnemonic in cls.instrs_store_update:
            assert isinstance(args[0], Register) and size is not None
            is_store = True
            if mnemonic.endswith("x"):
                assert (
                    len(args) == 3 + psq_imms
                    and isinstance(args[1], Register)
                    and isinstance(args[2], Register)
                )
                inputs = [args[0], args[1], args[2]]
                outputs = [args[1]]

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    store = cls.instrs_store_update[mnemonic](a)

                    # Update the register in the second argument
                    update = a.reg_ref(1)
                    offset = a.reg(2)
                    s.set_reg(update, add_imm(update, a.regs[update], offset, a))

                    if store is not None:
                        s.store_memory(store, a.reg_ref(0))

            else:
                assert len(args) == 2 + psq_imms and isinstance(args[1], AsmAddressMode)
                inputs = [args[0], args[1].base]
                outputs = make_memory_access(args[1], size) + [args[1].base]

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    store = cls.instrs_store_update[mnemonic](a)

                    # Update the register in the second argument
                    update = a.memory_ref(1)
                    if not isinstance(update, AddressMode):
                        raise DecompFailure(
                            f"Unhandled store-and-update arg in {instr_str}: {update!r}"
                        )
                    s.set_reg(
                        update.base,
                        add_imm(
                            update.base, a.regs[update.base], Literal(update.offset), a
                        ),
                    )

                    if store is not None:
                        s.store_memory(store, a.reg_ref(0))

        elif mnemonic in cls.instrs_load:
            assert isinstance(args[0], Register) and size is not None
            if mnemonic.endswith("x"):
                assert (
                    len(args) == 3 + psq_imms
                    and isinstance(args[1], Register)
                    and isinstance(args[2], Register)
                )
                inputs = [args[1], args[2]]
            else:
                assert len(args) == 2 + psq_imms
                if isinstance(args[1], AsmAddressMode):
                    inputs = make_memory_access(args[1], size) + [args[1].base]
            is_load = True
            outputs = [args[0]]
            eval_fn = lambda s, a: s.set_reg(a.reg_ref(0), cls.instrs_load[mnemonic](a))
        elif mnemonic in cls.instrs_load_update:
            assert isinstance(args[0], Register) and size is not None
            is_load = True
            if mnemonic.endswith("x"):
                assert (
                    len(args) == 3 + psq_imms
                    and isinstance(args[1], Register)
                    and isinstance(args[2], Register)
                )
                inputs = [args[1], args[2]]
                outputs = [args[0], args[1]]

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    target = a.reg_ref(0)
                    val = cls.instrs_load_update[mnemonic](a)
                    s.set_reg(target, val)
                    # In `rD, rA, rB`, update `rA = rA + rB`
                    update_reg = a.reg_ref(1)
                    offset = a.reg(2)
                    if update_reg == target:
                        raise DecompFailure(
                            f"Invalid instruction, rA and rD must be different in {instr_str}"
                        )
                    s.set_reg(
                        update_reg, add_imm(update_reg, a.regs[update_reg], offset, a)
                    )

            else:
                assert len(args) == 2 + psq_imms and isinstance(args[1], AsmAddressMode)
                inputs = make_memory_access(args[1], size) + [args[1].base]
                outputs = [args[0], args[1].base]

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    target = a.reg_ref(0)
                    val = cls.instrs_load_update[mnemonic](a)
                    s.set_reg(target, val)
                    # In `rD, rA(N)`, update `rA = rA + N`
                    update = a.memory_ref(1)
                    if not isinstance(update, AddressMode):
                        raise DecompFailure(
                            f"Unhandled load-and-update arg in {instr_str}: {update!r}"
                        )
                    update_reg = update.base
                    offset = Literal(update.offset)
                    if update_reg == target:
                        raise DecompFailure(
                            f"Invalid instruction, rA and rD must be different in {instr_str}"
                        )
                    s.set_reg(
                        update_reg, add_imm(update_reg, a.regs[update_reg], offset, a)
                    )

        elif mnemonic in ("stmw", "lmw"):
            assert (
                len(args) == 2
                and isinstance(args[0], Register)
                and isinstance(args[1], AsmAddressMode)
                and args[0].register_name[0] == "r"
            )
            is_store = mnemonic == "stmw"
            index = int(args[0].register_name[1:])
            offset = args[1].addend_as_literal()
            while index <= 31:
                reg = Register(f"r{index}")
                mem = make_memory_access(
                    AsmAddressMode(
                        base=args[1].base,
                        addend=AsmLiteral(offset),
                        writeback=None,
                    ),
                    4,
                )
                if mnemonic == "stmw":
                    inputs.append(reg)
                    outputs.extend(mem)
                else:
                    outputs.append(reg)
                    inputs.extend(mem)
                index += 1
                offset += 4
            inputs.append(args[1].base)
            # TODO: These are only supported in function prologues/epilogues
            eval_fn = None
        elif mnemonic in cls.instrs_no_dest:
            assert not any(isinstance(a, (AsmAddressMode)) for a in args)
            inputs = [r for r in args if isinstance(r, Register)]
            eval_fn = lambda s, a: s.write_statement(cls.instrs_no_dest[mnemonic](a))
        elif mnemonic.rstrip(".") in cls.instrs_destination_first:
            assert isinstance(args[0], Register)
            outputs = [args[0]]
            if mnemonic == "mflr":
                assert len(args) == 1
                inputs = [Register("lr")]
            elif mnemonic == "mfctr":
                assert len(args) == 1
                inputs = [Register("ctr")]
            elif mnemonic.rstrip(".") == "rlwimi":
                assert (
                    len(args) == 5
                    and isinstance(args[1], Register)
                    and not isinstance(args[2], (Register, AsmAddressMode))
                    and not isinstance(args[3], (Register, AsmAddressMode))
                    and not isinstance(args[4], (Register, AsmAddressMode))
                )
                inputs = [args[0], args[1]]
            elif mnemonic.startswith("cvt."):
                assert isinstance(args[1], Register)
                if len(args) == 2:
                    inputs = [args[1]]
                else:
                    assert isinstance(args[2], AsmAddressMode)
                    size = 8
                    inputs = make_memory_access(args[2], size) + [args[1], args[2].base]
            else:
                assert not any(isinstance(a, AsmAddressMode) for a in args)
                inputs = [r for r in args[1:] if isinstance(r, Register)]
            if mnemonic.rstrip(".") in ("adde", "addme", "addze", "subfe", "subfze"):
                inputs.append(Register("carry"))
            if mnemonic.rstrip(".") in ("addc", "addic", "subfc", "subfic"):
                outputs.append(Register("carry"))
            if mnemonic.endswith("."):
                # Instructions ending in `.` update the condition reg
                outputs.extend(cr0_bits)

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                target = a.reg_ref(0)
                val = cls.instrs_destination_first[mnemonic.rstrip(".")](a)
                target_val = s.set_reg(target, val)
                # Instructions ending in `.` update the condition reg
                if mnemonic.endswith("."):
                    s.set_reg(
                        Register("cr0_eq"),
                        BinaryOp.icmp(
                            target_val, "==", Literal(0, type=target_val.type)
                        ),
                    )
                    # Use manual casts for cr0_gt/cr0_lt so that the type of target_val is not modified
                    # until the resulting bit is .use()'d.
                    target_s32 = Cast(
                        target_val, reinterpret=True, silent=True, type=Type.s32()
                    )
                    s.set_reg(
                        Register("cr0_gt"),
                        BinaryOp(target_s32, ">", Literal(0), type=Type.s32()),
                    )
                    s.set_reg(
                        Register("cr0_lt"),
                        BinaryOp(target_s32, "<", Literal(0), type=Type.s32()),
                    )
                    s.set_reg(
                        Register("cr0_so"),
                        fn_op("M2C_OVERFLOW", [target_val], type=Type.s32()),
                    )
                if Register("carry") in outputs:
                    s.set_reg(
                        Register("carry"),
                        fn_op("M2C_CARRY", [target_val], type=Type.s32()),
                    )

        elif mnemonic in ("mtctr", "mtlr"):
            assert len(args) == 1 and isinstance(args[0], Register)
            dest_reg = Register(mnemonic[2:])
            inputs = [args[0]]
            outputs = [dest_reg]
            eval_fn = lambda s, a: s.set_reg(dest_reg, a.reg(0))
        elif mnemonic == "loopstructcopy.fictive":
            assert len(args) == 9
            assert isinstance(args[0], Register)
            assert isinstance(args[1], Register)
            assert isinstance(args[2], Register)
            assert isinstance(args[3], Register)
            assert isinstance(args[4], Register)
            width = args[5]
            bias = args[6]
            adj = args[7]
            tail_size = args[8]
            inputs = [args[2], args[3], args[4]]
            outputs = [args[0], args[1]]

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                assert isinstance(width, AsmLiteral)
                assert isinstance(bias, AsmLiteral)
                assert isinstance(adj, AsmLiteral)
                assert isinstance(tail_size, AsmLiteral)
                tail = tail_size.value
                offset = bias.value - adj.value
                dst = dst_out = a.reg(2)
                src = src_out = a.reg(3)
                if offset != 0:
                    dst = BinaryOp.int(dst, "+", Literal(offset))
                    src = BinaryOp.int(src, "+", Literal(offset))
                if adj.value != 0:
                    dst_out = BinaryOp.int(dst_out, "-", Literal(adj.value))
                    src_out = BinaryOp.int(src_out, "-", Literal(adj.value))
                count = a.reg(4)
                if isinstance(count, Literal):
                    incr_size = Literal(count.value * width.value)
                    copy_size = Literal(count.value * width.value + tail)
                else:
                    incr_size = BinaryOp.int(count, "*", Literal(width.value))
                    copy_size = incr_size
                    if tail != 0:
                        copy_size = BinaryOp.int(copy_size, "+", Literal(tail))
                s.write_statement(void_fn_op("M2C_STRUCT_COPY", [dst, src, copy_size]))
                dst_out = handle_add_real(dst_out, incr_size, a)
                src_out = handle_add_real(src_out, incr_size, a)
                s.set_reg_real(a.reg_ref(0), dst_out, function_return=True)
                s.set_reg_real(a.reg_ref(1), src_out, function_return=True)

        elif mnemonic in cls.instrs_ppc_compare:
            assert len(args) == 3 and isinstance(args[1], Register)
            assert isinstance(args[0], Register)
            inputs = [r for r in args[1:] if isinstance(r, Register)]
            outputs = list(cr_field_bits(args[0]))

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                # Write the field's 4 bits; fictives keep them live for cror.
                cr_lt, cr_gt, cr_eq, cr_so = cr_field_bits(args[0])
                if mnemonic in ("fcmpo.so.gte.fictive", "fcmpo.so.lte.fictive"):
                    s.set_reg(cr_so, cls.instrs_ppc_compare[mnemonic](a, "=="))
                    s.set_reg(cr_eq, cls.instrs_ppc_compare["fcmpu"](a, "=="))
                    s.set_reg(cr_gt, cls.instrs_ppc_compare["fcmpu"](a, ">"))
                    s.set_reg(cr_lt, cls.instrs_ppc_compare["fcmpu"](a, "<"))
                    return
                s.set_reg(cr_eq, cls.instrs_ppc_compare[mnemonic](a, "=="))
                s.set_reg(cr_gt, cls.instrs_ppc_compare[mnemonic](a, ">"))
                s.set_reg(cr_lt, cls.instrs_ppc_compare[mnemonic](a, "<"))
                s.set_reg(cr_so, Literal(0))

        elif mnemonic in cls.instrs_ignore:
            pass
        elif mnemonic in ("mfspr", "mtspr", "mfsprg", "mtsprg"):
            # mfspr/mtspr; mfsprg/mtsprg use a GQR index 0-7. Emit hook calls.
            is_mfspr = mnemonic in ("mfspr", "mfsprg")
            if is_mfspr:
                assert len(args) == 2 and isinstance(args[0], Register)
                spr_arg = args[1]
                outputs = [args[0]]
            else:
                assert len(args) == 2 and isinstance(args[1], Register)
                spr_arg = args[0]
                inputs = [args[1]]
            spr = cls._spr_number(spr_arg)
            if mnemonic in ("mfsprg", "mtsprg") and spr is not None and spr < 8:
                # Literal GQR index -> GQR SPR number (912 + index).
                spr += 912
            if spr is not None:
                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    if is_mfspr:
                        s.set_reg(
                            a.reg_ref(0),
                            fn_op(
                                "M2C_MFSPR",
                                [Literal(spr, type=Type.u32())],
                                type=Type.u32(),
                            ),
                        )
                    else:
                        s.write_statement(
                            void_fn_op(
                                "M2C_MTSPR",
                                [Literal(spr, type=Type.u32()), a.reg(1)],
                            )
                        )
            else:
                # Unrecognized SPR name/number: fall back to the generic
                # unknown-instruction error.
                if args and isinstance(args[0], Register):
                    inputs = [r for r in args[1:] if isinstance(r, Register)]
                    outputs = [args[0]]
                    maybe_dest_first = True
                else:
                    maybe_dest_first = False

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    error = ErrorExpr(f"unknown instruction: {instr_str}")
                    if maybe_dest_first:
                        s.set_reg_real(
                            a.reg_ref(0), error, emit_exactly_once=True
                        )
                    else:
                        s.write_statement(ExprStmt(error))

        elif mnemonic in ("mfxer", "mtxer", "mffs", "mtfsf"):
            # XER/FPSCR access; emit hook calls.
            if mnemonic in ("mfxer", "mffs"):
                assert len(args) == 1 and isinstance(args[0], Register)
                outputs = [args[0]]
                op = "M2C_MFXER" if mnemonic == "mfxer" else "M2C_MFFS"

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    s.set_reg(
                        a.reg_ref(0),
                        fn_op(op, [], type=Type.u32()),
                    )
            else:
                # mtxer rS / mtfsf mask, fB
                assert len(args) >= 1 and isinstance(args[-1], Register)
                inputs = [args[-1]]
                src_index = len(args) - 1
                if mnemonic == "mtxer":
                    op = "M2C_MTXER"

                    def eval_fn(s: NodeState, a: InstrArgs) -> None:
                        s.write_statement(
                            void_fn_op(op, [a.reg(src_index)])
                        )
                else:
                    assert isinstance(args[0], AsmLiteral)
                    op = "M2C_MTFSF"
                    mask = args[0].value

                    def eval_fn(s: NodeState, a: InstrArgs) -> None:
                        s.write_statement(
                            void_fn_op(
                                op,
                                [
                                    Literal(mask, type=Type.u32()),
                                    a.reg(src_index),
                                ],
                            )
                        )

        elif mnemonic in ("mfcr", "mtcrf", "mcrf"):
            # CR moves; emit hook calls.
            if mnemonic == "mfcr":
                assert len(args) == 1 and isinstance(args[0], Register)
                outputs = [args[0]]

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    s.set_reg(
                        a.reg_ref(0),
                        fn_op("M2C_MFCR", [], type=Type.u32()),
                    )
            elif mnemonic == "mtcrf":
                assert len(args) == 2 and isinstance(args[1], Register)
                assert isinstance(args[0], AsmLiteral)
                mask = args[0].value
                inputs = [args[1]]

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    s.write_statement(
                        void_fn_op(
                            "M2C_MTCRF",
                            [Literal(mask, type=Type.u32()), a.reg(1)],
                        )
                    )
            else:
                # mcrf copies a CR field; fields are not tracked as values.
                assert len(args) == 2
                assert all(isinstance(arg, Register) for arg in args)
                fields = []
                for arg in args:
                    assert isinstance(arg, Register)
                    name = arg.register_name
                    assert name.startswith("cr") and name[2:].isdigit()
                    fields.append(int(name[2:]))

                def eval_fn(s: NodeState, a: InstrArgs) -> None:
                    s.write_statement(
                        void_fn_op(
                            "M2C_MCRF",
                            [Literal(fields[0]), Literal(fields[1])],
                        )
                    )

        elif mnemonic.startswith("ps_"):
            # Paired-single SIMD; emit M2C_PS_* hook calls. psq_l/st above.
            assert args and isinstance(args[0], Register)
            assert all(isinstance(r, Register) for r in args)
            outputs = [args[0]]
            inputs = [r for r in args[1:] if isinstance(r, Register)]
            op = "M2C_PS_" + mnemonic[3:].upper()

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                s.set_reg(
                    a.reg_ref(0),
                    fn_op(op, [a.reg(i) for i in range(1, len(args))], type=Type.f64()),
                )

        else:
            # If the mnemonic is unsupported, guess if it is destination-first
            if args and isinstance(args[0], Register):
                inputs = [r for r in args[1:] if isinstance(r, Register)]
                outputs = [args[0]]
                maybe_dest_first = True
            else:
                maybe_dest_first = False
            if mnemonic.endswith("."):
                outputs.extend(cr0_bits)

            def eval_fn(s: NodeState, a: InstrArgs) -> None:
                error = ErrorExpr(f"unknown instruction: {instr_str}")
                if mnemonic.endswith("."):
                    # Unimplemented instructions that modify CR0
                    s.set_reg(Register("cr0_eq"), error)
                    s.set_reg(Register("cr0_gt"), error)
                    s.set_reg(Register("cr0_lt"), error)
                    s.set_reg(Register("cr0_so"), error)
                if maybe_dest_first:
                    s.set_reg_real(a.reg_ref(0), error, emit_exactly_once=True)
                else:
                    s.write_statement(ExprStmt(error))

        return Instruction(
            mnemonic=mnemonic,
            args=args,
            meta=meta,
            inputs=inputs,
            clobbers=clobbers,
            outputs=outputs,
            jump_target=jump_target,
            function_target=function_target,
            is_conditional=is_conditional,
            is_return=is_return,
            is_load=is_load,
            is_store=is_store,
            eval_fn=eval_fn,
        )

    ir_patterns = [
        LoopStructCopySetupPattern(),
        FloatishToSintIrPattern(),
        SintToDoubleIrPattern(),
        UintToDoubleIrPattern(),
        SintToFloatIrPattern(),
        UintToFloatIrPattern(),
        CmpnezPattern1(),
        CmpnezPattern2(),
        CmplezPattern(),
        CmpgtzPattern(),
        CmpnePattern(),
        CmplePattern(),
        CmpltPattern1(),
        CmpltPattern2(),
        CmpleuPattern1(),
        CmpleuPattern2(),
        CmpltuPattern1(),
        CmpltuPattern2(),
    ]

    asm_patterns = [
        FcmpuCrorSoPattern(),
        FcmpoCrorPattern(),
        MfcrPattern(),
        TailCallPattern(),
        SaveRestoreRegsFnPattern(),
        LoopStructCopyPattern(),
        BranchCtrPattern(),
        FloatishToUintPattern(),
        StructCopyPattern(),
    ]

    instrs_ignore: Set[str] = {
        "nop",
        # Assume stmw/lmw are only used for saving/restoring saved regs
        "stmw",
        "lmw",
        # `{crclr,crset} 6` are used as part of the ABI for floats & varargs
        # For now, we can ignore them (and later use them to help in function_abi)
        "crclr",
        "crset",
        # Cache hints; dcbz is not a hint and is not ignored.
        "dcbt",
        "dcbst",
        "dcbtst",
        "dcbf",
        "dcbi",
        "icbi",
        "icbt",
    }

    instrs_store: StoreInstrMap = {
        "stb": lambda a: make_store(a, type=Type.int_of_size(8)),
        "sth": lambda a: make_store(a, type=Type.int_of_size(16)),
        "stw": lambda a: make_store(a, type=Type.reg32(likely_float=False)),
        "stbx": lambda a: make_storex(a, type=Type.int_of_size(8)),
        "sthx": lambda a: make_storex(a, type=Type.int_of_size(16)),
        "stwx": lambda a: make_storex(a, type=Type.reg32(likely_float=False)),
        # TODO: Do we need to model the truncation from f64 to f32 here?
        "stfs": lambda a: make_store(a, type=Type.f32()),
        "stfd": lambda a: make_store(a, type=Type.f64()),
        "stfsx": lambda a: make_storex(a, type=Type.f32()),
        "stfdx": lambda a: make_storex(a, type=Type.f64()),
        "psq_st": lambda a: make_store(a, type=Type.f64()),
    }
    instrs_store_update: StoreInstrMap = {
        "stbu": lambda a: make_store(a, type=Type.int_of_size(8)),
        "sthu": lambda a: make_store(a, type=Type.int_of_size(16)),
        "stwu": lambda a: make_store(a, type=Type.reg32(likely_float=False)),
        "stbux": lambda a: make_storex(a, type=Type.int_of_size(8)),
        "sthux": lambda a: make_storex(a, type=Type.int_of_size(16)),
        "stwux": lambda a: make_storex(a, type=Type.reg32(likely_float=False)),
        "stfsu": lambda a: make_store(a, type=Type.f32()),
        "stfdu": lambda a: make_store(a, type=Type.f64()),
        "stfsux": lambda a: make_storex(a, type=Type.f32()),
        "stfdux": lambda a: make_storex(a, type=Type.f64()),
    }
    instrs_load: InstrMap = {
        "lba": lambda a: handle_load(a, type=Type.s8()),
        "lbz": lambda a: handle_load(a, type=Type.u8()),
        "lha": lambda a: handle_load(a, type=Type.s16()),
        "lhz": lambda a: handle_load(a, type=Type.u16()),
        "lwz": lambda a: handle_load(a, type=Type.reg32(likely_float=False)),
        "lbax": lambda a: handle_loadx(a, type=Type.s8()),
        "lbzx": lambda a: handle_loadx(a, type=Type.u8()),
        "lhax": lambda a: handle_loadx(a, type=Type.s16()),
        "lhzx": lambda a: handle_loadx(a, type=Type.u16()),
        "lwzx": lambda a: handle_loadx(a, type=Type.reg32(likely_float=False)),
        # TODO: Do we need to model the promotion from f32 to f64 here?
        "lfs": lambda a: handle_load(a, type=Type.f32()),
        "lfd": lambda a: handle_load(a, type=Type.f64()),
        "lfsx": lambda a: handle_loadx(a, type=Type.f32()),
        "lfdx": lambda a: handle_loadx(a, type=Type.f64()),
        "psq_l": lambda a: handle_load(a, type=Type.f64()),
    }
    instrs_load_update: InstrMap = {
        "lbau": lambda a: handle_load(a, type=Type.s8()),
        "lbzu": lambda a: handle_load(a, type=Type.u8()),
        "lhau": lambda a: handle_load(a, type=Type.s16()),
        "lhzu": lambda a: handle_load(a, type=Type.u16()),
        "lwzu": lambda a: handle_load(a, type=Type.reg32(likely_float=False)),
        "lbaux": lambda a: handle_loadx(a, type=Type.s8()),
        "lbzux": lambda a: handle_loadx(a, type=Type.u8()),
        "lhaux": lambda a: handle_loadx(a, type=Type.s16()),
        "lhzux": lambda a: handle_loadx(a, type=Type.u16()),
        "lwzux": lambda a: handle_loadx(a, type=Type.reg32(likely_float=False)),
        "lfsu": lambda a: handle_load(a, type=Type.f32()),
        "lfdu": lambda a: handle_load(a, type=Type.f64()),
        "lfsux": lambda a: handle_loadx(a, type=Type.f32()),
        "lfdux": lambda a: handle_loadx(a, type=Type.f64()),
    }

    instrs_branches: Dict[str, str] = {
        # Branch instructions/pseudoinstructions
        # Technically `bge` is defined as `cr0_gt || cr0_eq`; not as `!cr0_lt`
        # This assumption may not hold if the bits are modified with instructions like
        # `crand` which modify individual bits in CR.
        # The `!` indicates that the condition in the register is negated
        "beq": "cr0_eq",
        "bge": "!cr0_lt",
        "bgt": "cr0_gt",
        "ble": "!cr0_gt",
        "blt": "cr0_lt",
        "bne": "!cr0_eq",
        "bns": "!cr0_so",
        "bso": "cr0_so",
        "bdnz": "ctr",
        "bdz": "!ctr",
        "bdnz.fictive": "ctr",
        "bdz.fictive": "!ctr",
    }
    instrs_no_dest: StmtInstrMap = {
        "sync": lambda a: void_fn_op("M2C_SYNC", []),
        "isync": lambda a: void_fn_op("M2C_SYNC", []),
        "structcopy.fictive": lambda a: void_fn_op(
            "M2C_STRUCT_COPY", [a.reg(0), a.reg(1), a.full_imm(2)]
        ),
    }

    instrs_dest_first_non_load: InstrMap = {
        # Integer arithmetic
        # TODO: Read XER_CA in extended instrs, instead of using CarryBit
        "add": lambda a: handle_add(a),
        "addc": lambda a: handle_add(a),
        "adde": lambda a: carry_add_to(handle_add(a)),
        "addze": lambda a: carry_add_to(a.reg(1)),
        "addi": lambda a: handle_addi(
            a.reg_ref(0), a.reg_ref(1), a.reg(1), a.s16_imm(2), a
        ),
        "addic": lambda a: handle_addi(
            a.reg_ref(0), a.reg_ref(1), a.reg(1), a.s16_imm(2), a
        ),
        "addis": lambda a: handle_addis(a),
        "subf": lambda a: fold_divmod(BinaryOp.intptr(a.reg(2), "-", a.reg(1))),
        "subfc": lambda a: fold_divmod(BinaryOp.intptr(a.reg(2), "-", a.reg(1))),
        "subfe": lambda a: carry_sub_from(
            fold_divmod(BinaryOp.intptr(a.reg(2), "-", a.reg(1)))
        ),
        "subfic": lambda a: fold_divmod(BinaryOp.intptr(a.s16_imm(2), "-", a.reg(1))),
        "subfze": lambda a: carry_sub_from(
            fold_mul_chains(UnaryOp.sint("-", a.reg(1))),
        ),
        "neg": lambda a: fold_mul_chains(UnaryOp.sint("-", a.reg(1))),
        "divw": lambda a: BinaryOp.sint(a.reg(1), "/", a.reg(2)),
        "divwu": lambda a: BinaryOp.uint(a.reg(1), "/", a.reg(2)),
        "mulli": lambda a: BinaryOp.int(a.reg(1), "*", a.s16_imm(2)),
        "mullw": lambda a: BinaryOp.int(a.reg(1), "*", a.reg(2)),
        "mulhw": lambda a: fold_divmod(BinaryOp.int(a.reg(1), "MULT_HI", a.reg(2))),
        "mulhwu": lambda a: fold_divmod(BinaryOp.int(a.reg(1), "MULTU_HI", a.reg(2))),
        # Bit arithmetic
        "or": lambda a: handle_or(a.reg(1), a.reg(2)),
        "orc": lambda a: handle_or(
            a.reg(1), UnaryOp("~", a.reg(2), type=Type.intish())
        ),
        "ori": lambda a: handle_or(a.reg(1), a.u16_imm(2)),
        "oris": lambda a: handle_or(a.reg(1), a.shifted_u16_imm(2)),
        "and": lambda a: BinaryOp.int(a.reg(1), "&", a.reg(2)),
        "andc": lambda a: BinaryOp.int(
            a.reg(1), "&", UnaryOp("~", a.reg(2), type=Type.intish())
        ),
        "not": lambda a: UnaryOp("~", a.reg(1), type=Type.intish()),
        "nor": lambda a: UnaryOp(
            "~", BinaryOp.int(a.reg(1), "|", a.reg(2)), type=Type.intish()
        ),
        "xor": lambda a: BinaryOp.int(a.reg(1), "^", a.reg(2)),
        "eqv": lambda a: UnaryOp(
            "~", BinaryOp.int(a.reg(1), "^", a.reg(2)), type=Type.intish()
        ),
        "andi": lambda a: BinaryOp.int(a.reg(1), "&", a.u16_imm(2)),
        "andis": lambda a: BinaryOp.int(a.reg(1), "&", a.shifted_u16_imm(2)),
        "xori": lambda a: handle_xor(a.reg(1), a.u16_imm(2)),
        "xoris": lambda a: BinaryOp.int(a.reg(1), "^", a.shifted_u16_imm(2)),
        "cmpnez.fictive": lambda a: handle_cmpnez(a.reg(1)),
        "cmpne.fictive": lambda a: BinaryOp.icmp(a.reg(1), "!=", a.reg(2)),
        "cmple.fictive": lambda a: BinaryOp.scmp(a.reg(1), "<=", a.reg(2)),
        "cmplez.fictive": lambda a: BinaryOp.scmp(a.reg(1), "<=", Literal(0)),
        "cmplt.fictive": lambda a: BinaryOp.scmp(a.reg(1), "<", a.reg(2)),
        "cmpgtz.fictive": lambda a: BinaryOp.scmp(a.reg(1), ">", Literal(0)),
        "cmpleu.fictive": lambda a: BinaryOp.ucmp(a.reg(1), "<=", a.reg(2)),
        "cmpltu.fictive": lambda a: BinaryOp.ucmp(a.reg(1), "<", a.reg(2)),
        "rlwimi": lambda a: handle_rlwimi(
            a.reg(0), a.reg(1), a.imm_value(2), a.imm_value(3), a.imm_value(4)
        ),
        "rlwinm": lambda a: handle_rlwinm(
            a.reg(1), a.imm_value(2), a.imm_value(3), a.imm_value(4)
        ),
        "rlwnm": lambda a: handle_rlwnm(
            a.reg(1), a.reg(2), a.imm_value(3), a.imm_value(4)
        ),
        "slw": lambda a: fold_mul_chains(
            BinaryOp.int(a.reg(1), "<<", as_intish(a.reg(2)))
        ),
        "srw": lambda a: fold_divmod(BinaryOp.ushift(a.reg(1), ">>", a.reg(2))),
        "sraw": lambda a: fold_divmod(BinaryOp.sshift(a.reg(1), ">>", a.reg(2))),
        "srawi": lambda a: handle_shift_right(a, signed=True),
        "extsb": lambda a: as_type(a.reg(1), Type.s8(), silent=False),
        "extsh": lambda a: as_type(a.reg(1), Type.s16(), silent=False),
        "cntlzw": lambda a: UnaryOp("CLZ", a.reg(1), type=Type.intish()),
        # Load Immediate
        "li": lambda a: a.full_imm(1),
        "lis": lambda a: load_upper(a),
        # Move from Special Register
        "mflr": lambda a: a.regs[Register("lr")],
        "mfctr": lambda a: a.regs[Register("ctr")],
        # Move pseudoinstructions
        "mr": lambda a: a.reg(1),
        "move.fictive": lambda a: a.reg(1),
        # Floating Point Arithmetic
        "fadd": lambda a: handle_add_double(a),
        "fadds": lambda a: handle_add_float(a),
        "fdiv": lambda a: BinaryOp.f64(a.reg(1), "/", a.reg(2)),
        "fdivs": lambda a: BinaryOp.f32(a.reg(1), "/", a.reg(2)),
        "fmul": lambda a: BinaryOp.f64(a.reg(1), "*", a.reg(2)),
        "fmuls": lambda a: BinaryOp.f32(a.reg(1), "*", a.reg(2)),
        "fsub": lambda a: BinaryOp.f64(a.reg(1), "-", a.reg(2)),
        "fsubs": lambda a: BinaryOp.f32(a.reg(1), "-", a.reg(2)),
        "fneg": lambda a: UnaryOp("-", a.reg(1), type=Type.floatish()),
        "fmr": lambda a: a.reg(1),
        "frsp": lambda a: handle_convert(a.reg(1), Type.f32(), Type.f64()),
        "fctiwz": lambda a: handle_convert(a.reg(1), Type.sintish(), Type.floatish()),
        "fctiwz.fictive": lambda a: handle_convert(
            a.reg(1), Type.sintish(), Type.floatish()
        ),
        "cvt.u.d.fictive": lambda a: handle_convert(
            a.reg(1), Type.uintish(), Type.floatish()
        ),
        "cvt.d.i.fictive": lambda a: handle_convert(
            a.reg(1), Type.f64(), Type.sintish()
        ),
        "cvt.d.u.fictive": lambda a: handle_convert(
            a.reg(1), Type.f64(), Type.uintish()
        ),
        "cvt.s.i.fictive": lambda a: handle_convert(
            a.reg(1), Type.f32(), Type.sintish()
        ),
        "cvt.s.u.fictive": lambda a: handle_convert(
            a.reg(1), Type.f32(), Type.uintish()
        ),
        # Floating Poing Fused Multiply-{Add,Sub}
        "fmadd": lambda a: BinaryOp.f64(
            BinaryOp.f64(a.reg(1), "*", a.reg(2)), "+", a.reg(3)
        ),
        "fmadds": lambda a: BinaryOp.f32(
            BinaryOp.f32(a.reg(1), "*", a.reg(2)), "+", a.reg(3)
        ),
        "fnmadd": lambda a: UnaryOp(
            "-",
            BinaryOp.f64(BinaryOp.f64(a.reg(1), "*", a.reg(2)), "+", a.reg(3)),
            type=Type.f64(),
        ),
        "fnmadds": lambda a: UnaryOp(
            "-",
            BinaryOp.f32(BinaryOp.f32(a.reg(1), "*", a.reg(2)), "+", a.reg(3)),
            type=Type.f32(),
        ),
        "fmsub": lambda a: BinaryOp.f64(
            BinaryOp.f64(a.reg(1), "*", a.reg(2)), "-", a.reg(3)
        ),
        "fmsubs": lambda a: BinaryOp.f32(
            BinaryOp.f32(a.reg(1), "*", a.reg(2)), "-", a.reg(3)
        ),
        "fnmsub": lambda a: UnaryOp(
            "-",
            BinaryOp.f64(BinaryOp.f64(a.reg(1), "*", a.reg(2)), "-", a.reg(3)),
            type=Type.f64(),
        ),
        "fnmsubs": lambda a: UnaryOp(
            "-",
            BinaryOp.f32(BinaryOp.f32(a.reg(1), "*", a.reg(2)), "-", a.reg(3)),
            type=Type.f32(),
        ),
        # TODO: Detect if we should use fabs or fabsf
        "fabs": lambda a: fn_op("fabs", [a.reg(1)], Type.floatish()),
        "fres": lambda a: fn_op("__fres", [a.reg(1)], Type.floatish()),
        "frsqrte": lambda a: fn_op("__frsqrte", [a.reg(1)], Type.floatish()),
        "fsel": lambda a: TernaryOp(
            BinaryOp.fcmp(a.reg(1), ">=", Literal(0)),
            a.reg(2),
            a.reg(3),
            type=Type.floatish(),
        ),
    }
    instrs_destination_first: InstrMap = {
        **instrs_dest_first_non_load,
        **instrs_load,
    }

    instrs_ppc_compare: Dict[str, Callable[[InstrArgs, str], Expression]] = {
        # Integer (signed/unsigned)
        "cmpw": lambda a, op: BinaryOp.sintptr_cmp(a.reg(1), op, a.reg(2)),
        "cmpwi": lambda a, op: BinaryOp.sintptr_cmp(a.reg(1), op, a.s16_imm(2)),
        "cmplw": lambda a, op: BinaryOp.uintptr_cmp(a.reg(1), op, a.reg(2)),
        "cmplwi": lambda a, op: BinaryOp.uintptr_cmp(a.reg(1), op, a.s16_imm(2)),
        # Floating point
        # TODO: There is a difference in how these two instructions handle NaN
        "fcmpo": lambda a, op: BinaryOp.fcmp(a.reg(1), op, a.reg(2)),
        "fcmpu": lambda a, op: BinaryOp.fcmp(a.reg(1), op, a.reg(2)),
        "fcmpo.lte.fictive": lambda a, op: BinaryOp.fcmp(
            a.reg(1), op if op != "==" else "<=", a.reg(2)
        ),
        "fcmpo.gte.fictive": lambda a, op: BinaryOp.fcmp(
            a.reg(1), op if op != "==" else ">=", a.reg(2)
        ),
        # fcmpo.so.{lte, gte}.fictive: the fcmpu; cror idiom folded to >= / <=.
        "fcmpo.so.gte.fictive": lambda a, op: BinaryOp.fcmp(
            a.reg(1), ">=", a.reg(2)
        ),
        "fcmpo.so.lte.fictive": lambda a, op: BinaryOp.fcmp(
            a.reg(1), "<=", a.reg(2)
        ),
    }

    def arg_name(self, loc: ArgLoc) -> str:
        if loc.offset is not None:
            return f"arg_sp{format_hex(loc.offset)}"
        assert loc.reg is not None
        reg_num = int(loc.reg.register_name[1:])
        if loc.reg.register_name.startswith("r"):
            return f"arg{reg_num - 3}"
        else:
            return f"farg{reg_num - 1}"

    # Duplicated by MipseeArch.function_abi
    @staticmethod
    def function_abi(
        fn_sig: FunctionSignature,
        likely_regs: Dict[Register, bool],
        *,
        for_call: bool,
    ) -> Abi:
        known_slots: List[AbiArgSlot] = []
        candidate_slots: List[AbiArgSlot] = []

        # $rX & $fX regs can be interspersed in function args, unlike in the MIPS O32 ABI
        intptr_regs = [r for r in PpcArch.argument_regs if r.register_name[0] != "f"]
        float_regs = [r for r in PpcArch.argument_regs if r.register_name[0] == "f"]

        if fn_sig.params_known:
            ind = 0
            stack_offset = 0
            for param in fn_sig.params:
                # TODO: Support structs as parameters/return type, and 64-bit values
                # passed on the stack.
                param_type = param.type.decay()
                reg: Optional[Register] = None
                offset: Optional[int] = None
                try:
                    if param_type.is_float():
                        reg = float_regs.pop(0)
                    else:
                        reg = intptr_regs.pop(0)
                except IndexError:
                    # Stack variable
                    offset = stack_offset
                    stack_offset += 4
                known_slots.append(
                    AbiArgSlot(ArgLoc(offset, ind, reg), param_type, name=param.name)
                )
                ind += 1
            if fn_sig.is_variadic:
                for reg in intptr_regs:
                    candidate_slots.append(
                        AbiArgSlot(ArgLoc(None, ind, reg), Type.intptr())
                    )
                    ind += 1
                for reg in float_regs:
                    candidate_slots.append(
                        AbiArgSlot(ArgLoc(None, ind, reg), Type.floatish())
                    )
                    ind += 1
        else:
            for ind, reg in enumerate(PpcArch.argument_regs):
                if reg.register_name[0] != "f":
                    candidate_slots.append(
                        AbiArgSlot(ArgLoc(None, ind, reg), Type.intptr())
                    )
                else:
                    candidate_slots.append(
                        AbiArgSlot(ArgLoc(None, ind, reg), Type.floatish())
                    )

        valid_extra_regs: Set[Register] = {
            slot.loc.reg for slot in known_slots if slot.loc.reg is not None
        }
        possible_slots: List[AbiArgSlot] = []
        for slot in candidate_slots:
            reg = slot.loc.reg
            if reg is None or reg not in likely_regs:
                continue

            # Don't pass this register if lower numbered ones are undefined.
            if slot == candidate_slots[0]:
                # For varargs, a subset of regs may be used. Don't check
                # earlier registers for the first member of that subset.
                pass
            else:
                # Only r3-r10/f1-f13 can be used for arguments
                regname = reg.register_name
                prev_reg = Register(f"{regname[0]}{int(regname[1:])-1}")
                if (
                    prev_reg in PpcArch.argument_regs
                    and prev_reg not in valid_extra_regs
                ):
                    continue

            valid_extra_regs.add(reg)

            # Skip registers that are untouched from the initial parameter
            # list. This is sometimes wrong (can give both false positives
            # and negatives), but having a heuristic here is unavoidable
            # without access to function signatures, or when dealing with
            # varargs functions. Decompiling multiple functions at once
            # would help.
            # TODO: don't do this in the middle of the argument list
            if not likely_regs[reg]:
                continue

            possible_slots.append(slot)

        return Abi(
            arg_slots=known_slots,
            possible_slots=possible_slots,
        )

    @staticmethod
    def function_return(expr: Expression) -> Dict[Register, Expression]:
        return {
            Register("f1"): Cast(
                expr, reinterpret=True, silent=True, type=Type.floatish()
            ),
            Register("r3"): Cast(
                expr, reinterpret=True, silent=True, type=Type.intptr()
            ),
            Register("r4"): as_u32(
                Cast(expr, reinterpret=True, silent=False, type=Type.u64())
            ),
        }
