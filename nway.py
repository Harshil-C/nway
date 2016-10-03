#!/usr/bin/env python
# -*- coding: utf-8 -*-

__doc__ = """Multiway association between astrometric catalogue. Use --help for usage.

Example: nway.py --radius 10 --prior-completeness 0.95 --mag GOODS:mag_H auto --mag IRAC:mag_irac1 auto cdfs4Ms_srclist_v3.fits :Pos_error CANDELS_irac1.fits 0.5 gs_short.fits 0.1 --out=out.fits
"""

import sys
import numpy
from numpy import log10, pi, exp, logical_and
import matplotlib.pyplot as plt
import astropy.io.fits as pyfits
import argparse
import nwaylib.fastskymatch as match
import nwaylib.bayesdistance as bayesdist
import nwaylib.magnitudeweights as magnitudeweights

# set up program arguments

class HelpfulParser(argparse.ArgumentParser):
	def error(self, message):
		sys.stderr.write('error: %s\n' % message)
		self.print_help()
		sys.exit(2)

parser = HelpfulParser(description=__doc__,
	epilog="""Johannes Buchner (C) 2013-2016 <johannes.buchner.acad@gmx.com>""",
	formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--radius', type=float, required=True,
	help='exclusive search radius in arcsec for initial matching')

parser.add_argument('--mag-radius', default=None, type=float,
	help='search radius for magnitude histograms. By default, the Bayesian posterior is used.')

#nx/(1887*15e+18)
parser.add_argument('--prior-completeness', metavar='COMPLETENESS', default=1, type=float,
	help='expected matching completeness of sources (prior)')

parser.add_argument('--mag', metavar='MAGCOLUMN+MAGFILE', type=str, nargs=2, action='append', default=[],
	help="""name of <table>:<column> for magnitude biasing, and filename for magnitude histogram 
	(use auto for auto-computation within mag-radius).
	Example: --mag GOODS:mag_H auto --mag IRAC:mag_irac1 irac_histogram.txt""")

parser.add_argument('--acceptable-prob', metavar='PROB', type=float, default=0.005,
	help='limit up to which secondary solutions are flagged')

parser.add_argument('--min-prob', type=float, default=0,
	help='lowest probability allowed in final catalogue. If 0, no trimming is performed.')

parser.add_argument('--out', metavar='OUTFILE', help='output file name', required=True)

parser.add_argument('catalogues', type=str, nargs='+',
	help="""input catalogue fits files and position errors.
	Example: cdfs4Ms_srclist_v3.fits :Pos_error CANDELS_irac1.fits 0.5 gs_short.fits 0.1
	""")


# parsing arguments
args = parser.parse_args()

print 'nway arguments:'

diff_secondary = args.acceptable_prob
outfile = args.out

filenames = args.catalogues[::2]
print '    catalogues: ', ', '.join(filenames)
pos_errors = args.catalogues[1::2]
print '    position errors/columns: ', ', '.join(pos_errors)

fits_tables = []
table_names = []
tables = []
source_densities = []
source_densities_plus = []
fits_formats = []
for fitsname in filenames:
	fits_table = pyfits.open(fitsname)[1]
	fits_tables.append(fits_table)
	table_name = fits_table.name
	table_names.append(table_name)
	table = fits_table.data
	fits_formats.append([c.format for c in fits_table.columns])
	tables.append(table)

	n = len(table)
	assert 'SKYAREA' in fits_table.header, 'file "%s", table "%s" does not have a field "SKYAREA", which should contain the area of the catalogue in square degrees' % (fitsname, table_name)
	area = fits_table.header['SKYAREA'] # in square degrees
	area_total = (4 * pi * (180 / pi)**2)
	density = n / area * area_total
	print '      from catalogue "%s" (%d), density is %e' % (table_name, n, density)
	# this takes into account that the source may be absent
	density_plus = (n + 1) / area * area_total
	source_densities.append(density)
	source_densities_plus.append(density_plus)

# source can not be absent in primary catalogue
source_densities_plus[0] = source_densities[0]
min_prob = args.min_prob

match_radius = args.radius / 60. / 60 # in degrees
#if args.mag_radius is not None:
#	mag_radius = match_radius # in arc sec
#else:
mag_radius = args.mag_radius # in arc sec

magnitude_columns = args.mag
print '    magnitude columns: ', ', '.join([c for c, _ in magnitude_columns])

# first match input catalogues, compute possible combinations in match_radius
results, columns, match_header = match.match_multiple(tables, table_names, match_radius, fits_formats)
table = pyfits.BinTableHDU.from_columns(pyfits.ColDefs(columns)).data

assert len(table) > 0, 'No matches.'

# first pass: find secure matches and secure non-matches

print 'finalizing catalogue'

print '    finding position error columns ...'
# get the separation and error columns for the bayesian weighting
errors    = []
for table_name, pos_error in zip(table_names, pos_errors):
	if pos_error[0] == ':':
		# get column
		k = "%s_%s" % (table_name, pos_error[1:])
		assert k in table.dtype.names, 'ERROR: Position error column for "%s" not in table "%s". Have these columns: %s' % (k, table_name, ', '.join(table.dtype.names))
		print '    Position error for "%s": found column %s: Values are [%f..%f]' % (table_name, k, table[k].min(), table[k].max())
		if table[k].min() <= 0:
			print 'WARNING: Some separation errors in "%s" are 0! This will give invalid results (%d rows).' % (k, (table[k] <= 0).sum())
		if table[k].max() > match_radius * 60 * 60:
			print 'WARNING: Some separation errors in "%s" are larger than the match radius! Increase --radius to >> %s' % (k, table[k].max())
		errors.append(table[k])
	else:
		print '    Position error for "%s": using fixed value %f' % (table_name, float(pos_error))
		if float(pos_error) > match_radius * 60 * 60:
			print 'WARNING: Given separation error for "%s" is larger than the match radius! Increase --radius to >> %s' % (k, float(pos_error))
		errors.append(float(pos_error) * numpy.ones(len(table)))

print '    finding position columns ...'
# table is in arcsec, and therefore separations is in arcsec
separations = []
for ti, a in enumerate(table_names):
	row = []
	for tj, b in enumerate(table_names):
		if ti < tj:
			k = 'Separation_%s_%s' % (b, a)
			assert k in table.dtype.names, 'ERROR: Separation column for "%s" not in merged table. Have columns: %s' % (k, ', '.join(table.dtype.names))
			row.append(table[k])
		else:
			row.append(numpy.ones(len(table)) * numpy.nan)
	separations.append(row)

print '    computing probabilities from separations ...'
# compute n-way position evidence

log_bf = numpy.zeros(len(table)) * numpy.nan
prior = numpy.zeros(len(table)) * numpy.nan
# handle all cases (also those with missing counterparts in some catalogues)
for case in range(2**(len(table_names)-1)):
	table_mask = numpy.array([True] + [(case / 2**(ti)) % 2 == 0 for ti in range(len(tables)-1)])
	ncat = table_mask.sum()
	# select those cases
	mask = True
	for i in range(1, len(tables)):
		if table_mask[i]: # require not nan
			mask = numpy.logical_and(mask, -numpy.isnan(separations[0][i]))
		else:
			mask = numpy.logical_and(mask, numpy.isnan(separations[0][i]))
	
	# select errors
	errors_selected = [e[mask] for e, m in zip(errors, table_mask) if m]
	separations_selected = [[cell[mask] for cell, m in zip(row, table_mask) if m] 
		for row, m in zip(separations, table_mask) if m]
	log_bf[mask] = bayesdist.log_bf(separations_selected, errors_selected)

	prior[mask] = source_densities[0] * args.prior_completeness / numpy.product(numpy.asarray(source_densities_plus)[table_mask])

post = bayesdist.posterior(prior, log_bf)

# add the additional columns
columns.append(pyfits.Column(name='bf', format='E', array=log_bf))
columns.append(pyfits.Column(name='bfpost', format='E', array=post))

# find magnitude biasing functions
biases = {}
for mag, magfile in magnitude_columns:
	print 'magnitude bias "%s" ...' % mag
	table_name, col_name = mag.split(':', 1)
	assert table_name in table_names, 'table name specified for magnitude ("%s") unknown. Known tables: %s' % (table_name, ', '.join(table_names))
	ti = table_names.index(table_name)
	col_names = tables[ti].dtype.names
	assert col_name in col_names, 'column name specified for magnitude ("%s") unknown. Known columns in table "%s": %s' % (mag, table_name, ', '.join(col_names))
	ci = col_names.index(col_name)
	
	res = results[table_name]
	res_defined = results[table_name] != -1
	
	# get magnitudes of all
	mag_all = tables[ti][col_name]
	# mark -99 as undefined
	mag_all[mag_all == -99] = numpy.nan
	
	# get magnitudes of selected
	mask_all = -numpy.logical_or(numpy.isnan(mag_all), numpy.isinf(mag_all))

	col = "%s_%s" % (table_name, col_name)
	
	if magfile == 'auto':
		if mag_radius is not None:
			if mag_radius >= match_radius * 60 * 60:
				print 'WARNING: magnitude radius is very large (>= matching radius). Consider using a smaller value.'
			selection = table['Separation_max'] < mag_radius
			#mask_radius = table['Separation_max'] < mag_radius
			selection_possible = selection
		else:
			selection = post > 0.9
			selection_possible = post > 0.01
		
		# ignore cases where counterpart is missing
		selection = numpy.logical_and(selection, res_defined)
		selection_possible = numpy.logical_and(selection_possible, res_defined)
		
		#print '   selection', selection.sum(), selection_possible.sum(), (-selection_possible).sum()
		
		rows = list(set(results[table_name][selection]))
		assert len(rows) > 1, 'No magnitude values within mag_radius for "%s".' % mag
		mag_sel = mag_all[rows]
		
		# remove vaguely possible options from alternative histogram
		rows_possible = list(set(results[table_name][selection_possible]))
		mask_others = mask_all.copy()
		mask_others[rows_possible] = False
		
		mask_sel = -numpy.logical_or(numpy.isnan(mag_sel), numpy.isinf(mag_sel))

		#print '      non-nans: ', mask_sel.sum(), mask_others.sum()

		print 'magnitude histogram of column "%s": %d secure matches, %d insecure matches and %d secure non-matches of %d total entries (%d valid)' % (col, mask_sel.sum(), len(rows_possible), mask_others.sum(), len(mag_all), mask_all.sum())
		
		# make function fitting to ratio shape
		bins, hist_sel, hist_all = magnitudeweights.adaptive_histograms(mag_all[mask_all], mag_sel[mask_sel])
		with open(mag.replace(':', '_') + '_fit.txt', 'wb') as f:
			f.write(b'# lo hi selected others\n')
			numpy.savetxt(f,
				numpy.transpose([bins[:-1], bins[1:], hist_sel, hist_all]), 
				fmt = ["%10.5f"]*4)
	else:
		print 'magnitude histogramming: using histogram from "%s" for column "%s"' % (magfile, col)
		bins_lo, bins_hi, hist_sel, hist_all = numpy.loadtxt(magfile).transpose()
		bins = numpy.array(list(bins_lo) + [bins_hi[-1]])
	func = magnitudeweights.fitfunc_histogram(bins, hist_sel, hist_all)
	magnitudeweights.plot_fit(bins, hist_sel, hist_all, func, mag)
	weights = log10(func(table[col]))
	# undefined magnitudes do not contribute
	weights[numpy.isnan(weights)] = 0
	biases[col] = weights


# add the bias columns
for col, weights in biases.iteritems():
	columns.append(pyfits.Column(name='bias_%s' % col, format='E', array=10**weights))


# add the posterior column
total = log_bf + sum(biases.values())
post = bayesdist.posterior(prior, total)
columns.append(pyfits.Column(name='post', format='E', array=post))


# flagging of solutions. Go through groups by primary id (IDs in first catalogue)
index = numpy.zeros_like(post)
prob_no_match = numpy.zeros_like(post)
prob_this_match = numpy.zeros_like(post)

primary_id_key = match.get_tablekeys(tables[0], 'ID')
primary_id_key = '%s_%s' % (table_names[0], primary_id_key)
match_header['COL_PRIM'] = primary_id_key
match_header['COLS_ERR'] = ' '.join(['%s_%s' % (ti, poscol) for ti, poscol in zip(table_names, pos_errors)])
print '    grouping by column "%s" for flagging ...' % (primary_id_key)

primary_ids = sorted(set(table[primary_id_key]))

for primary_id in primary_ids:
	# group
	mask = table[primary_id_key] == primary_id
	group_posterior = post[mask]
	best_index = group_posterior.argmax()
	best_val = group_posterior[best_index]
	
	# flag second best
	mask2 = logical_and(mask, best_val - post < diff_secondary)
	# ignore very poor solutions
	mask2 = logical_and(mask2, post > 0.1)
	index[mask2] = 2
	# flag best
	mask1 = logical_and(mask, best_val == post)
	index[mask1] = 1
	
	# compute no-match probability
	offset = total[mask].max()
	bfsum = log10((10**(total[mask] - offset)).sum()) + offset
	prob_no_match[mask] = 1 - bayesdist.posterior(prior[mask], bfsum)
	prob_this_match[mask] = 10**(total[mask] - bfsum)
	
columns.append(pyfits.Column(name='post_group_no_match', format='E', array=prob_no_match))
columns.append(pyfits.Column(name='post_group_this_match', format='E', array=prob_this_match))

# add the flagging column
columns.append(pyfits.Column(name='match_flag', format='I', array=index))

# cut away poor posteriors if requested
if min_prob > 0:
	mask = -(post < min_prob)
	print '    cutting away %d (below minimum)' % (len(mask) - mask.sum())

	for c in columns:
		c.array = c.array[mask]


# write out fits file
tbhdu = pyfits.BinTableHDU.from_columns(pyfits.ColDefs(columns))
print 'writing "%s" (%d rows, %d columns)' % (outfile, len(tbhdu.data), len(columns))

hdulist = match.wraptable2fits(tbhdu, 'MULTIMATCH')
hdulist[0].header['METHOD'] = 'multi-way matching'
hdulist[0].header['INPUT'] = ', '.join(filenames)
hdulist[0].header['TABLES'] = ', '.join(table_names)
hdulist[0].header['BIASING'] =  ', '.join(biases.keys())
hdulist[0].header['NWAYCMD'] = ' '.join(sys.argv)
for k, v in args.__dict__.iteritems():
	hdulist[0].header.add_comment("argument %s: %s" % (k, v))
hdulist[0].header.update(match_header)
hdulist.writeto(outfile, clobber=True)




