#!/usr/bin/env python
"""
map bisulfite converted reads to an insilico converted genome using bwa mem OR bwa mem2.
A command to this program like:

    python bwameth.py --reference ref.fa A.fq B.fq

Gets converted to:

    bwa mem -pCMR ref.fa.bwameth.c2t '<python bwameth.py c2t A.fq B.fq'
    OR
    bwa-mem2 mem -pCMR ref.fa.bwameth.c2t '<python bwameth.py c2t A.fq B.fq'

So that A.fq has C's converted to T's and B.fq has G's converted to A's
and both are streamed directly to the aligner without a temporary file
producing standard SAM output. 

Index from BWA-MEM or BWA-MEM2 is auto detected and the corresponding aligner is chosen.

Indexing:
bwa-meth supports indexes from BWA-MEM and BWA-MEM2.

    bwameth.py index $REF #For BWA-MEM (default)
    OR
    bwameth.py index-mem2 $REF #For BWA-MEM2
"""
from __future__ import print_function
import tempfile
import sys
import os
import os.path as op
from subprocess import Popen, PIPE
import argparse
from subprocess import check_call
from operator import itemgetter
from itertools import groupby, repeat, chain, islice
import re

try:
    from itertools import izip
    import string
    maketrans = string.maketrans
except ImportError: # python3
    izip = zip
    maketrans = str.maketrans
import toolshed
from toolshed import nopen, reader, is_newer_b

__version__ = "0.2.7"

def nopen_keep_parent_stdin(f, mode="r"):

    if f.startswith("|"):
        # using shell explicitly makes things like process substitution work:
        # http://stackoverflow.com/questions/7407667/python-subprocess-subshells-and-redirection
        # use sys.stderr so we dont have to worry about checking it...
        p = Popen(f[1:], stdout=PIPE, stdin=sys.stdin,
                  stderr=sys.stderr if mode == "r" else PIPE,
                  shell=True, bufsize=-1, # use system default for buffering
                  preexec_fn=toolshed.files.prefunc,
                  close_fds=False, executable=os.environ.get('SHELL'))
        if sys.version_info[0] > 2:
            import io
            p.stdout = io.TextIOWrapper(p.stdout)
            p.stdin = io.TextIOWrapper(sys.stdin)
            if mode != "r":
                p.stderr = io.TextIOWrapper(p.stderr)

        if mode and mode[0] == "r":
            return toolshed.files.process_iter(p, f[1:])
        return p
    else:
        return toolshed.files.nopen(f,mode)

def checkX(cmd):
    for p in os.environ['PATH'].split(":"):
        if os.access(os.path.join(p, cmd), os.X_OK):
            break
    else:
        raise Exception("executable for '%s' not found" % cmd)

checkX('samtools')
#checkX('bwa')

class BWAMethException(Exception): pass

def comp(s, _comp=maketrans('ATCG', 'TAGC')):
    return s.translate(_comp)

def wrap(text, width=100): # much faster than textwrap
    try: xrange
    except NameError: xrange = range
    for s in xrange(0, len(text), width):
        yield text[s:s+width]

def run(cmd):
    list(nopen("|%s" % cmd.lstrip("|")))

def fasta_iter(fasta_name):
    fh = nopen(fasta_name)
    faiter = (x[1] for x in groupby(fh, lambda line: line[0] == ">"))
    for header in faiter:
        header = next(header)[1:].strip()
        yield header, "".join(s.strip() for s in next(faiter)).upper()

def convert_reads(fq1s, fq2s, out=sys.stdout):

    for fq1, fq2 in zip(fq1s.split(","), fq2s.split(",")):
        sys.stderr.write("converting reads in %s,%s\n" % (fq1, fq2))
        fq1 = nopen(fq1)

        #examines first five lines to detect if this is an interleaved fastq file
        first_five = list(islice(fq1, 5))

        r1_header = first_five[0]
        r2_header = first_five[-1]

        if r1_header.split(' ')[0] == r2_header.split(' ')[0]:
            already_interleaved = True
        else:
            already_interleaved = False

        q1_iter = izip(*[chain.from_iterable([first_five,fq1])] * 4)

        if fq2 != "NA":
            fq2 = nopen(fq2)
            q2_iter = izip(*[fq2] * 4)
        else:
            if already_interleaved:
                sys.stderr.write("detected interleaved fastq\n")
            else:
                sys.stderr.write("WARNING: running bwameth in single-end mode\n")
            q2_iter = repeat((None, None, None, None))

        lt80 = 0

        if already_interleaved:
            selected_iter = q1_iter
        else:
            selected_iter = chain.from_iterable(izip(q1_iter, q2_iter))

        for read_i, (name, seq, _, qual) in enumerate(selected_iter):
            if name is None: continue
            convert_and_write_read(name,seq,qual,read_i%2,out)
            if len(seq) < 80:
                lt80 += 1

    out.flush()
    if lt80 > 50:
        sys.stderr.write("WARNING: %i reads with length < 80\n" % lt80)
        sys.stderr.write("       : this program is designed for long reads\n")
    return 0

def convert_and_write_read(name,seq,qual,read_i,out):

    name = name.rstrip("\r\n").split(" ")[0]
    if name[0] != "@":
        sys.stderr.write("""ERROR!!!!
    ERROR!!! FASTQ conversion failed
    ERROR!!! expecting FASTQ 4-tuples, but found a record %s that doesn't start with "@"
    """ % name)
        sys.exit(1)
    if name.endswith(("_R1", "_R2")):
        name = name[:-3]
    elif name.endswith(("/1", "/2")):
        name = name[:-2]

    seq = seq.upper().rstrip('\n')


    char_a, char_b = ['CT', 'GA'][read_i]
    # keep original sequence as name.
    name = " ".join((name,
                     "YS:Z:" + seq +
                     "\tYC:Z:" + char_a + char_b + '\n'))
    seq = seq.replace(char_a, char_b)
    out.write("".join((name, seq, "\n+\n", qual)))

def convert_fasta(ref_fasta, just_name=False):
    out_fa = ref_fasta + ".bwameth.c2t"
    if just_name:
        return out_fa
    msg = "c2t in %s to %s" % (ref_fasta, out_fa)
    if is_newer_b(ref_fasta, out_fa):
        sys.stderr.write("already converted: %s\n" % msg)
        return out_fa
    sys.stderr.write("converting %s\n" % msg)
    try:
        fh = open(out_fa, "w")
        for header, seq in fasta_iter(ref_fasta):
            ########### Reverse ######################
            fh.write(">r%s\n" % header)

            #if non_cpg_only:
            #    for ctx in "TAG": # use "ATC" for fwd
            #        seq = seq.replace('G' + ctx, "A" + ctx)
            #    for line in wrap(seq):
            #        print >>fh, line
            #else:
            for line in wrap(seq.replace("G", "A")):
                fh.write(line + '\n')

            ########### Forward ######################
            fh.write(">f%s\n" % header)
            for line in wrap(seq.replace("C", "T")):
                fh.write(line + '\n')
        fh.close()
    except:
        try:
            fh.close()
        except UnboundLocalError:
            pass
        os.unlink(out_fa)
        raise
    return out_fa


def bwa_index(fa, ver = "mem"):

    if ver == "mem":
        if is_newer_b(fa, (fa + '.amb', fa + '.sa')):
            return
        sys.stderr.write("indexing with bwa-mem: %s\n" % fa)
        try:
            run("bwa index -a bwtsw %s" % fa)
        except:
            if op.exists(fa + ".amb"):
                os.unlink(fa + ".amb")
            raise
    else:
        if is_newer_b(fa, (fa + '.amb', fa + '.pac')):
            return
        sys.stderr.write("indexing with bwa-mem2: %s\n" % fa)
        try:
            run("bwa-mem2 index %s" % fa)
        except:
            if op.exists(fa + ".amb"):
                os.unlink(fa + ".amb")
            raise

class Bam(object):
    __slots__ = 'read flag chrom pos mapq cigar chrom_mate pos_mate tlen \
            seq qual other'.split()
    def __init__(self, args):
        for a, v in zip(self.__slots__[:11], args):
            setattr(self, a, v)
        self.other = args[11:]
        self.flag = int(self.flag)
        self.pos = int(self.pos)
        self.tlen = int(float(self.tlen))

    def __repr__(self):
        return "Bam({chr}:{start}:{read}".format(chr=self.chrom,
                                                 start=self.pos,
                                                 read=self.read)

    def __str__(self):
        return "\t".join(str(getattr(self, s)) for s in self.__slots__[:11]) \
                         + "\t" + "\t".join(self.other)

    def is_first_read(self):
        return bool(self.flag & 0x40)

    def is_second_read(self):
        return bool(self.flag & 0x80)

    def is_plus_read(self):
        return not (self.flag & 0x10)

    def is_minus_read(self):
        return bool(self.flag & 0x10)

    def is_mapped(self):
        return not (self.flag & 0x4)

    def cigs(self):
        if self.cigar == "*":
            yield (0, None)
            raise StopIteration
        cig_iter = groupby(self.cigar, lambda c: c.isdigit())
        for g, n in cig_iter:
            yield int("".join(n)), "".join(next(cig_iter)[1])

    def cig_len(self):
        return sum(c[0] for c in self.cigs() if c[1] in
                   ("M", "D", "N", "EQ", "X", "P"))

    def left_shift(self):
        left = 0
        for n, cig in self.cigs():
            if cig == "M": break
            if cig == "H":
                left += n
        return left

    def right_shift(self):
        right = 0
        for n, cig in reversed(list(self.cigs())):
            if cig == "M": break
            if cig == "H":
                right += n
        return -right or None

    @property
    def original_seq(self):
        try:
            return next(x for x in self.other if x.startswith("YS:Z:"))[5:]
        except:
            sys.stderr.write(repr(self.other) + "\n")
            sys.stderr.write(self.read + "\n")
            raise

    @property
    def ga_ct(self):
        return [x for x in self.other if x.startswith("YC:Z:")]

    def longest_match(self, patt=re.compile(r"\d+M")):
        return max(int(x[:-1]) for x in patt.findall(self.cigar))


def rname(fq1, fq2=""):
    fq1, fq2 = fq1.split(",")[0], fq2.split(",")[0]
    def name(f):
        n = op.basename(op.splitext(f)[0])
        if n.endswith('.fastq'): n = n[:-6]
        if n.endswith(('.fq', '.r1', '.r2')): n = n[:-3]
        return n
    if fq2 == '':
        return name(fq1)
    else:
        return "".join(a for a, b in zip(name(fq1), name(fq2)) if a == b) or 'bm'


def bwa_mem(fa, fq_convert_cmd, extra_args, threads=1, rg=None,
            paired=True, set_as_failed=None, do_not_penalize_chimeras=False):
    conv_fa = convert_fasta(fa, just_name=True)
    # Currently use bwa-mem only
    idx = "mem1"
    # if is_newer_b(conv_fa, (conv_fa + '.amb', conv_fa + '.sa')):
    #     idx = "mem1"
    #     sys.stderr.write("--------------------\n")
    #     sys.stderr.write("Found BWA MEM index\n")
        
    # elif is_newer_b(conv_fa, (conv_fa + '.amb', conv_fa + '.pac')):
    #     idx = "mem2"
    #     sys.stderr.write("---------------------\n")
    #     sys.stderr.write("Found BWA MEM2 index\n")
        
    # else:
    #     raise BWAMethException("first run bwameth.py index %s OR bwameth.py index-mem2 %s OR make sure the modification time on the generated c2t files is newer than on the .fa file" % (fa, fa))


    if not rg is None and not rg.startswith('@RG'):
        rg = '@RG\\tID:{rg}\\tSM:{rg}'.format(rg=rg)

    #starts the pipeline with the program to convert fastqs
    cmd = ("|%s " % fq_convert_cmd)

    # penalize clipping and unpaired. lower penalty on mismatches (-B)
    if idx == "mem2":
        cmd += "|bwa-mem2 mem -T 40 -B 2 -L 10 -CM "
    else:
        cmd += "|bwa mem -T 40 -B 2 -L 10 -CM "

    if paired:
        cmd += ("-U 100 -p ")
    cmd += "-R '{rg}' -t {threads} {extra_args} {conv_fa} /dev/stdin"
    cmd = cmd.format(**locals())
    sys.stderr.write("running: %s\n" % cmd.lstrip("|"))
    sys.stderr.write("--------------------\n")
    as_bam(cmd, fa, set_as_failed, do_not_penalize_chimeras)


def as_bam(pfile, fa, set_as_failed=None, do_not_penalize_chimeras=False):
    """
    pfile: either a file or a |process to generate sam output
    fa: the reference fasta
    set_as_failed: None, 'f', or 'r'. If 'f'. Reads mapping to that strand
                      are given the sam flag of a failed QC alignment (0x200).
    """
    sam_iter = nopen_keep_parent_stdin(pfile, 'r')

    for line in sam_iter:
        if not line[0] == "@": break
        handle_header(line)
    else:
        sys.stderr.flush()
        raise Exception("bad or empty fastqs")
    sam_iter2 = (x.rstrip().split("\t") for x in chain([line], sam_iter))
    for read_name, pair_list in groupby(sam_iter2, itemgetter(0)):
        pair_list = [Bam(toks) for toks in pair_list]

        for aln in handle_reads(pair_list, set_as_failed, do_not_penalize_chimeras):
            sys.stdout.write(str(aln) + '\n')

def handle_header(line, out=sys.stdout):
    toks = line.rstrip().split("\t")
    if toks[0].startswith("@SQ"):
        sq, sn, ln = toks  # @SQ    SN:fchr11    LN:122082543
        # we have f and r, only print out f
        chrom = sn.split(":", 1)[1]
        if chrom.startswith('r'): return
        chrom = chrom[1:]
        toks = ["%s\tSN:%s\t%s" % (sq, chrom, ln)]
    if toks[0].startswith("@PG"):
        #out.write("\t".join(toks) + "\n")
        toks = ["@PG\tID:bwa-meth\tPN:bwa-meth\tVN:%s\tCL:\"%s\"" % (
                         __version__,
                         " ".join(x.replace("\t", "\\t") for x in sys.argv))]
    out.write("\t".join(toks) + "\n")


def handle_reads(alns, set_as_failed, do_not_penalize_chimeras):

    for aln in alns:
        orig_seq = aln.original_seq
        assert len(aln.seq) == len(aln.qual), aln.read
        # don't need this any more.
        aln.other = [x for x in aln.other if not x.startswith('YS:Z')]

        if not aln.is_mapped():
            aln.seq = orig_seq
            if len(aln.chrom) > 1 and aln.chrom[0] in 'fr':
                aln.chrom = aln.chrom[1:]
            continue

        # first letter of chrom is 'f' or 'r'
        direction = aln.chrom[0]
        aln.chrom = aln.chrom[1:]

        assert direction in 'fr', (direction, aln)
        aln.other.append('YD:Z:' + direction)

        if set_as_failed == direction:
            aln.flag |= 0x200

        if not do_not_penalize_chimeras:
        # here we have a heuristic that if the longest match is not 44% of the
        # sequence length, we mark it as failed QC and un-pair it. At the end
        # of the loop we set all members of this pair to be unmapped
            if aln.longest_match() < (len(orig_seq) * 0.44):
                aln.flag |= 0x200  # fail qc
                aln.flag &= (~0x2) # un-pair
                aln.mapq = min(int(aln.mapq), 1)

        mate_direction = aln.chrom_mate[0]
        if mate_direction not in "*=":
            aln.chrom_mate = aln.chrom_mate[1:]

        # adjust the original seq to the cigar
        l, r = aln.left_shift(), aln.right_shift()
        if aln.is_plus_read():
            aln.seq = orig_seq[l:r]
        else:
            aln.seq = comp(orig_seq[::-1][l:r])

    if any(aln.flag & 0x200 for aln in alns):
        for aln in alns:
            aln.flag |= 0x200
            aln.flag &= (~0x2)
    return alns

def cnvs_main(args):
    __doc__ = """
    calculate CNVs from BS-Seq bams or vcfs
    """
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--regions", help="optional target regions", default='NA')
    p.add_argument("bams", nargs="+")

    a = p.parse_args(args)
    r_script = """
options(stringsAsFactors=FALSE)
suppressPackageStartupMessages(library(cn.mops))
suppressPackageStartupMessages(library(snow))
args = commandArgs(TRUE)
regions = args[1]
bams = args[2:length(args)]
n = length(bams)
if(is.na(regions)){
    bam_counts = getReadCountsFromBAM(bams, parallel=min(n, 4), mode="paired")
    res = cn.mops(bam_counts, parallel=min(n, 4), priorImpact=20)
} else {
    segments = read.delim(regions, header=FALSE)
    gr = GRanges(segments[,1], IRanges(segments[,2], segments[,3]))
    bam_counts = getSegmentReadCountsFromBAM(bams, GR=gr, mode="paired", parallel=min(n, 4))
    res = exomecn.mops(bam_counts, parallel=min(n, 4), priorImpact=20)
}
res = calcIntegerCopyNumbers(res)

df = as.data.frame(cnvs(res))
write.table(df, row.names=FALSE, quote=FALSE, sep="\t")
"""
    with tempfile.NamedTemporaryFile(delete=True) as rfh:
        rfh.write(r_script + '\n')
        rfh.flush()
        for d in reader('|Rscript {rs_name} {regions} {bams}'.format(
            rs_name=rfh.name, regions=a.regions, bams=" ".join(a.bams)),
            header=False):
            print("\t".join(d))


def convert_fqs(fqs):
    script = __file__
    return "%s %s c2t %s %s" % (sys.executable, script, fqs[0],
               fqs[1] if len(fqs) > 1
                      else ','.join(['NA'] * len(fqs[0].split(","))))

def main(args=sys.argv[1:]):

    if len(args) > 0 and args[0] == "index":
        assert len(args) == 2, ("must specify fasta as 2nd argument")
        sys.exit(bwa_index(convert_fasta(args[1])))

    if len(args) > 0 and args[0] == "index-mem2":
        assert len(args) == 2, ("must specify fasta as 2nd argument")
        sys.exit(bwa_index(convert_fasta(args[1]), ver = "mem2"))

    if len(args) > 0 and args[0] == "c2t":
        sys.exit(convert_reads(args[1], args[2]))

    if len(args) > 0 and args[0] == "cnvs":
        sys.exit(cnvs_main(args[1:]))

    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--reference", help="reference fasta", required=True)
    p.add_argument("-t", "--threads", type=int, default=6)
    p.add_argument("--read-group", help="read-group to add to bam in same"
            " format as to bwa: '@RG\\tID:foo\\tSM:bar'")
    p.add_argument('--set-as-failed', help="flag alignments to this strand"
            " as not passing QC (0x200). Targetted BS-Seq libraries are often"
            " to a single strand, so we can flag them as QC failures. Note"
            " f == OT, r == OB. Likely, this will be 'f' as we will expect"
            " reads to align to the original-bottom (OB) strand and will flag"
            " as failed those aligning to the forward, or original top (OT).",
        default=None, choices=('f', 'r'))
    p.add_argument('-p', '--interleaved', action='store_true', help='fastq files have 4 lines of read1 followed by 4 lines of read2 (e.g. seqtk mergepe output)')
    p.add_argument('--version', action='version', version='bwa-meth.py {}'.format(__version__))

    p.add_argument("fastqs", nargs="+", help="bs-seq fastqs to align. Run"
            "multiple sets separated by commas, e.g. ... a_R1.fastq,b_R1.fastq"
            " a_R2.fastq,b_R2.fastq note that the order must be maintained.")

    # need to escape '%' in help text to avoid problems with --help,
    # see https://github.com/brentp/bwa-meth/issues/85
    p.add_argument('--do-not-penalize-chimeras', action='store_true', help="do not use the heuristic" 
            " that if the longest match is not 44%% of the sequence length, we mark"
            " it as failed QC and un-pair it, and set all members of pair to unmapped")

    args, pass_through_args = p.parse_known_args(args)

    # for the 2nd file. use G => A and bwa's support for streaming.
    conv_fqs_cmd = convert_fqs(args.fastqs)

    bwa_mem(args.reference, conv_fqs_cmd, ' '.join(map(str, pass_through_args)),
            threads=args.threads,
            rg=args.read_group or rname(*args.fastqs),
            paired=(len(args.fastqs) == 2 or args.interleaved),
            set_as_failed=args.set_as_failed,
            do_not_penalize_chimeras=args.do_not_penalize_chimeras)
    

if __name__ == "__main__":
    main(sys.argv[1:])
